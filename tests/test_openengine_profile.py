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
