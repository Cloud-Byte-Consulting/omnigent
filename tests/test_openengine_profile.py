"""Tests for the Open Engine stack-profile → session-policy loader (OE-1b Lane B).

Proves end-to-end that a profile's guardrails.policies (the OPA boundaries) become
session policies, and that the client-supplied profile name cannot escape the
profiles dir.
"""

from __future__ import annotations

import omnigent.server.profiles as prof


class _FakeStore:
    """Records create() calls instead of hitting a DB."""

    def __init__(self):
        self.created = []

    def create(self, **kw):
        self.created.append(kw)
        return None


def test_profile_name_from_labels():
    assert prof.profile_name_from_labels({"openengine.profile": "openengine_stack"}) == "openengine_stack"
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
    for bad in ["../secrets", "/etc/passwd", "a/b", "foo.bar", "", "..", "a b", "a;b", "A" * 100, "ok\n", "ok\nbad"]:
        assert prof.apply_profile_session_policies("conv", bad, store) == 0
    assert store.created == []  # nothing reached the store


def test_missing_profile_returns_zero():
    store = _FakeStore()
    assert prof.apply_profile_session_policies("conv", "no_such_profile_xyz", store) == 0
    assert store.created == []


def test_no_policy_store_returns_zero():
    assert prof.apply_profile_session_policies("conv", "openengine_stack", None) == 0


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
        _apply_openengine_profile_if_requested("conv_abc", {"openengine.profile": "openengine_stack"})

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


def test_helper_swallows_loader_exceptions():
    """A crash inside apply_profile_session_policies never propagates — session must not be orphaned."""
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
        # Must not raise.
        _apply_openengine_profile_if_requested("conv_abc", {"openengine.profile": "openengine_stack"})


# ── Gap documentation: bundle-create and terminal-create paths ────────────────
#
# These tests assert the ABSENCE of loader calls in the two uncovered paths.
# They are gap-markers: they PASS while the gap exists and will need updating
# when the gap is closed (add a real coverage test, delete this assertion).

def test_bundle_create_path_gap_no_loader_call():
    """GAP: _create_session_from_bundle and _persist_stored_session_bundle do not call
    apply_profile_session_policies. A bundle session with an openengine.profile label
    starts ungoverned until the JSON create path is used instead.

    This test PASSES while the gap exists. When the bundle path is wired to the
    loader, replace this with a coverage test and delete this assertion.
    """
    import inspect

    from omnigent.server.routes import sessions as sess_mod

    bundle_src = inspect.getsource(sess_mod._create_session_from_bundle)
    persist_src = inspect.getsource(sess_mod._persist_stored_session_bundle)
    # ponytail: source-grep gap marker — fails if someone adds the call without a real test
    assert "apply_profile_session_policies" not in bundle_src, (
        "_create_session_from_bundle now calls the loader — remove this gap marker "
        "and add a real coverage test for the bundle-create path."
    )
    assert "apply_profile_session_policies" not in persist_src, (
        "_persist_stored_session_bundle now calls the loader — remove this gap marker "
        "and add a real coverage test."
    )
    assert "_apply_openengine_profile_if_requested" not in bundle_src, (
        "_create_session_from_bundle now calls the helper — remove this gap marker."
    )


def test_terminal_create_path_gap_no_profile_injection_point():
    """GAP: create_session_terminal is a resource-create on an existing session, not
    a new-session create. The session's labels are not part of the terminal-create
    request body; there is no natural profile injection point.

    The bundle-create path also lacks the loader call. Both gaps are tracked here
    via a call-count assertion on the full sessions module: exactly ONE site calls
    _apply_openengine_profile_if_requested (inside _create_session_from_existing_agent,
    the JSON POST path). When either gap is closed, this count increases and the
    assertion fails — remove it and add a real coverage test.
    """
    import inspect

    from omnigent.server.routes import sessions as sess_mod

    # Full module source — _create_session_from_existing_agent is module-level,
    # the closure-internal endpoints call it indirectly.
    module_src = open(inspect.getfile(sess_mod)).read()
    # Count CALLS (has opening paren), excluding the definition line itself.
    # definition: "def _apply_openengine_profile_if_requested("  → ends with '('
    # calls: "_apply_openengine_profile_if_requested(conv.id, ..."
    call_sites = [
        line for line in module_src.splitlines()
        if "_apply_openengine_profile_if_requested(" in line
        and not line.lstrip().startswith("def ")
        and not line.lstrip().startswith("#")
    ]
    assert len(call_sites) == 1, (
        f"Expected exactly 1 call site for _apply_openengine_profile_if_requested "
        f"(only the JSON POST path); found {len(call_sites)}: {call_sites}. "
        "If the bundle or terminal path was wired, remove this gap marker and add "
        "real coverage tests for those paths."
    )
