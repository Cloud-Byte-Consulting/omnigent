"""Tests for the Open Engine stack-profile → session-policy loader (OE-1b Lane B).

Proves end-to-end that a profile's guardrails.policies (the OPA boundaries) become
session policies, and that the client-supplied profile name cannot escape the
profiles dir.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import omnigent.server.profiles as prof


class _FakeStore:
    """Records create() calls instead of hitting a DB."""

    def __init__(self):
        self.created = []
        self.deleted = []

    def create(self, **kw):
        self.created.append(kw)
        return

    def delete(self, policy_id, session_id):
        self.deleted.append((policy_id, session_id))
        return True


def test_profile_name_from_labels():
    assert (
        prof.profile_name_from_labels({"openengine.profile": "openengine_stack"})
        == "openengine_stack"
    )
    assert prof.profile_name_from_labels({}) is None
    assert prof.profile_name_from_labels(None) is None
    assert prof.profile_name_from_labels({"openengine.profile": ""}) is None


def test_apply_real_profile_attaches_opa():
    # Parses the real profiles/openengine_stack.yaml and attaches its
    # guardrails.policies as session policies.
    store = _FakeStore()
    n = prof.apply_profile_session_policies("conv_test", "openengine_stack", store)
    assert n == len(store.created) >= 1
    names = [c["name"] for c in store.created]
    handlers = [c["handler"] for c in store.created]
    assert "opa_oe_boundaries" in names
    assert "omnigent.policies.builtins.opa.opa_require_approval" in handlers
    assert all(c["type"] == "python" for c in store.created)
    assert all(c["session_id"] == "conv_test" for c in store.created)
    assert all(c["policy_id"].startswith("pol_") for c in store.created)


def test_github_and_jira_profiles_also_carry_opa():
    for name in ("openengine_stack_github", "openengine_stack_jira"):
        store = _FakeStore()
        prof.apply_profile_session_policies("conv", name, store)
        assert "omnigent.policies.builtins.opa.opa_require_approval" in [
            c["handler"] for c in store.created
        ], name


def test_unsafe_profile_name_rejected_no_store_calls():
    store = _FakeStore()
    for bad in [
        "../secrets",
        "/etc/passwd",
        "a/b",
        "foo.bar",
        "",
        "..",
        "a b",
        "a;b",
        "A" * 100,
        "ok\n",
        "ok\nbad",
    ]:
        with pytest.raises(prof.ProfileApplicationError):
            prof.apply_profile_session_policies("conv", bad, store)
    assert store.created == []  # nothing reached the store


def test_missing_profile_fails_closed():
    store = _FakeStore()
    with pytest.raises(prof.ProfileApplicationError):
        prof.apply_profile_session_policies("conv", "no_such_profile_xyz", store)
    assert store.created == []


def test_no_policy_store_fails_closed():
    with pytest.raises(prof.ProfileApplicationError):
        prof.apply_profile_session_policies("conv", "openengine_stack", None)


def test_create_failure_rolls_back_already_attached_policies(monkeypatch, tmp_path):
    """A profile is all-or-nothing when persistence fails partway through."""

    profile = tmp_path / "atomic.yaml"
    profile.write_text(
        """guardrails:
  policies:
    first:
      type: function
      on: [tool_call]
      function:
        path: omnigent.policies.builtins.opa.opa_require_approval
    second:
      type: function
      on: [tool_call]
      function:
        path: omnigent.policies.builtins.opa.opa_require_approval
"""
    )
    monkeypatch.setenv("OMNIGENT_PROFILES_DIR", str(tmp_path))

    class _FailSecondStore(_FakeStore):
        def create(self, **kw):
            if len(self.created) == 1:
                raise RuntimeError("db exploded")
            return super().create(**kw)

    store = _FailSecondStore()
    with pytest.raises(prof.ProfileApplicationError, match="db exploded"):
        prof.apply_profile_session_policies("conv", "atomic", store)

    assert len(store.created) == 1
    assert store.deleted == [(store.created[0]["policy_id"], "conv")]


# ── _apply_openengine_profile_if_requested (the sessions.py integration helper) ──
#
# These tests cover the wrapper that the JSON POST /v1/sessions create path calls.
# The helper is NOT called from the multipart bundle-create or terminal-create paths
# (see gap tests below).


def test_helper_calls_loader_when_label_present():
    """The helper delegates to apply_profile_session_policies with the right args."""
    from unittest.mock import MagicMock, patch

    from omnigent.server.routes.sessions import _apply_openengine_profile_if_requested

    fake_store = MagicMock()
    with (
        patch("omnigent.server.routes.sessions.get_policy_store", return_value=fake_store),
        patch("omnigent.server.profiles.apply_profile_session_policies") as m_apply,
    ):
        _apply_openengine_profile_if_requested(
            "conv_abc", {"openengine.profile": "openengine_stack"}
        )

    m_apply.assert_called_once_with("conv_abc", "openengine_stack", fake_store)


def test_helper_no_label_no_call():
    """The helper is a no-op when no openengine.profile label is set."""
    from unittest.mock import patch

    from omnigent.server.routes.sessions import _apply_openengine_profile_if_requested

    with patch("omnigent.server.profiles.apply_profile_session_policies") as m_apply:
        _apply_openengine_profile_if_requested("conv_abc", {})
        _apply_openengine_profile_if_requested("conv_abc", None)
        _apply_openengine_profile_if_requested("conv_abc", {"other_label": "val"})

    m_apply.assert_not_called()


def test_helper_propagates_loader_exceptions():
    """A requested profile cannot fail while session creation continues."""
    from unittest.mock import MagicMock, patch

    from omnigent.server.routes.sessions import _apply_openengine_profile_if_requested

    fake_store = MagicMock()
    with (
        patch("omnigent.server.routes.sessions.get_policy_store", return_value=fake_store),
        patch(
            "omnigent.server.profiles.apply_profile_session_policies",
            side_effect=RuntimeError("db exploded"),
        ),
    ):
        with pytest.raises(RuntimeError, match="db exploded"):
            _apply_openengine_profile_if_requested(
                "conv_abc", {"openengine.profile": "openengine_stack"}
            )


@pytest.mark.asyncio
async def test_create_guard_rolls_back_session_when_profile_fails():
    """A failed governance attachment removes the just-created session."""
    from unittest.mock import patch

    from omnigent.server.routes.sessions import _apply_profile_or_rollback_session

    class _Conversations:
        def __init__(self):
            self.deleted = []

        async def delete_conversation(self, session_id):
            self.deleted.append(session_id)
            return True

    conversations = _Conversations()
    with patch(
        "omnigent.server.routes.sessions._apply_openengine_profile_if_requested",
        side_effect=prof.ProfileApplicationError("profile failed"),
    ):
        with pytest.raises(prof.ProfileApplicationError, match="profile failed"):
            await _apply_profile_or_rollback_session(
                conversations,
                "conv_abc",
                {"openengine.profile": "openengine_stack"},
            )

    assert conversations.deleted == ["conv_abc"]


@pytest.mark.asyncio
async def test_create_guard_keeps_session_when_profile_succeeds():
    from unittest.mock import patch

    from omnigent.server.routes.sessions import _apply_profile_or_rollback_session

    class _Conversations:
        async def delete_conversation(self, session_id):
            raise AssertionError(f"unexpected rollback of {session_id}")

    with patch("omnigent.server.routes.sessions._apply_openengine_profile_if_requested"):
        await _apply_profile_or_rollback_session(
            _Conversations(),
            "conv_abc",
            {"openengine.profile": "openengine_stack"},
        )


# ── Gap documentation: bundle-create and terminal-create paths ────────────────
#
# These tests assert the ABSENCE of loader calls in the two uncovered paths.
# They are gap-markers: they PASS while the gap exists and will need updating
# when the gap is closed (add a real coverage test, delete this assertion).


def test_bundle_create_path_applies_profile():
    """The multipart/bundle create path applies the OpenEngine profile from the
    bundle's parsed labels — closing the earlier fail-open where a bundle session
    carrying ``openengine.profile`` was left ungoverned (the loader ran only on the
    JSON POST path).
    """
    import inspect

    from omnigent.server.routes import sessions as sess_mod

    module_src = Path(inspect.getfile(sess_mod)).read_text()
    assert "await _apply_profile_or_rollback_session(" in module_src, (
        "the multipart/bundle create path must call the loader with the bundle labels "
        "so bundle-created OE sessions are governed (mirrors the JSON POST path)."
    )


def test_governed_create_paths_count():
    """Exactly TWO new-session create paths apply the OpenEngine profile: the JSON
    POST path and the multipart/bundle path. The terminal-create path is a resource
    create on an EXISTING session (no new-session labels, no injection point), so it
    has no loader call by design. If a third site appears, confirm it is a real
    new-session create that should be governed and add coverage.
    """
    import inspect

    from omnigent.server.routes import sessions as sess_mod

    module_src = Path(inspect.getfile(sess_mod)).read_text()
    call_sites = [
        line
        for line in module_src.splitlines()
        if "_apply_profile_or_rollback_session(" in line
        and not line.lstrip().startswith(("def ", "async def "))
        and not line.lstrip().startswith("#")
    ]
    message = (
        "Expected 2 governed create paths (JSON POST + bundle); "
        f"found {len(call_sites)}: {call_sites}."
    )
    assert len(call_sites) == 2, message
