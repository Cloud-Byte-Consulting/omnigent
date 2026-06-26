"""Unit tests for omnigent.opa_delegate (OE-2 native-tool OPA delegation).

OPA is monkeypatched throughout, so these run with no live OPA server.
"""

from __future__ import annotations

import omnigent.opa_delegate as od


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_parse_native_tool_name_mcp():
    assert od.parse_native_tool_name("mcp__github__delete_repository") == (
        "github",
        "delete_repository",
    )


def test_parse_native_tool_name_mcp_underscored_tool():
    # The tool segment may contain single underscores; only "__" splits segments.
    assert od.parse_native_tool_name("mcp__resource_discovery__get_catalog_entry") == (
        "resource_discovery",
        "get_catalog_entry",
    )


def test_parse_native_tool_name_host_tool():
    assert od.parse_native_tool_name("Bash") == ("native", "Bash")


def test_build_opa_input_shape():
    inp = od.build_opa_input(
        "mcp__github__delete_repository", {"name": "x"}, groups=["g1"]
    )
    assert inp == {
        "server_name": "github",
        "tool_name": "delete_repository",
        "arguments": {"name": "x"},
        "groups": ["g1"],
    }


def test_build_opa_input_defaults():
    inp = od.build_opa_input("Bash", None)
    assert inp["server_name"] == "native"
    assert inp["arguments"] == {}
    assert inp["groups"] == []


def test_verdict_mapping():
    assert od.opa_verdict_to_action("allow") == od._ALLOW
    assert od.opa_verdict_to_action("deny") == od._DENY
    assert od.opa_verdict_to_action("require_approval") == od._ASK


def test_verdict_mapping_unknown_fails_closed():
    assert od.opa_verdict_to_action("surprise") == od._DENY
    assert od.opa_verdict_to_action(None) == od._DENY


def test_combine_deny_wins():
    assert od.combine_actions(od._ALLOW, od._DENY) == od._DENY
    assert od.combine_actions(od._DENY, od._ALLOW) == od._DENY
    assert od.combine_actions(od._ASK, od._ALLOW) == od._ASK
    assert od.combine_actions(od._ALLOW, od._ASK) == od._ASK
    assert od.combine_actions(od._DENY, od._ASK) == od._DENY
    assert od.combine_actions(od._ALLOW, od._ALLOW) == od._ALLOW


def test_delegate_mode_default_off(monkeypatch):
    monkeypatch.delenv(od._MODE_ENV, raising=False)
    assert od.delegate_mode() == "off"


def test_delegate_mode_unknown_is_off(monkeypatch):
    monkeypatch.setenv(od._MODE_ENV, "ENFROCE")  # typo
    assert od.delegate_mode() == "off"


def test_opa_decision_url_from_env(monkeypatch):
    monkeypatch.setenv(od._OPA_URL_ENV, "http://opa.internal:8181/")
    assert od.opa_decision_url() == "http://opa.internal:8181/v1/data/mcp/auth/oe_decision"


# ── opa_delegate_tool_call modes ─────────────────────────────────────────────


def _patch_opa(monkeypatch, decision):
    """Force query_opa_decision to return *decision* (or None) without HTTP."""
    monkeypatch.setattr(od, "query_opa_decision", lambda *a, **k: decision)


def test_off_returns_python_response_without_calling_opa(monkeypatch):
    monkeypatch.setenv(od._MODE_ENV, "off")
    calls = []
    monkeypatch.setattr(od, "query_opa_decision", lambda *a, **k: calls.append(1))
    py = {"result": od._ALLOW, "reason": None}
    out = od.opa_delegate_tool_call("Bash", {}, py)
    assert out is py
    assert calls == []  # OPA must not be consulted in off mode


def test_shadow_does_not_enforce_even_on_deny(monkeypatch):
    monkeypatch.setenv(od._MODE_ENV, "shadow")
    _patch_opa(monkeypatch, {"allow": False, "verdict": "deny", "reason": "boundary"})
    py = {"result": od._ALLOW, "reason": None}
    out = od.opa_delegate_tool_call("mcp__github__delete_repository", {}, py)
    assert out is py  # shadow observes only; Python verdict stands


def test_enforce_opa_deny_overrides_python_allow(monkeypatch):
    monkeypatch.setenv(od._MODE_ENV, "enforce")
    _patch_opa(monkeypatch, {"allow": False, "verdict": "deny", "reason": "boundary"})
    out = od.opa_delegate_tool_call(
        "mcp__github__delete_repository", {}, {"result": od._ALLOW, "reason": None}
    )
    assert out["result"] == od._DENY
    assert out["reason"] == "boundary"  # OPA's reason carried through


def test_enforce_require_approval_maps_to_ask(monkeypatch):
    monkeypatch.setenv(od._MODE_ENV, "enforce")
    _patch_opa(monkeypatch, {"allow": True, "verdict": "require_approval", "reason": "ask"})
    out = od.opa_delegate_tool_call(
        "mcp__github__publish_release", {}, {"result": od._ALLOW, "reason": None}
    )
    assert out["result"] == od._ASK


def test_enforce_keeps_python_deny_when_opa_allows(monkeypatch):
    monkeypatch.setenv(od._MODE_ENV, "enforce")
    _patch_opa(monkeypatch, {"allow": True, "verdict": "allow", "reason": "Approved"})
    out = od.opa_delegate_tool_call(
        "Bash", {}, {"result": od._DENY, "reason": "python-said-no"}
    )
    assert out["result"] == od._DENY
    assert out["reason"] == "python-said-no"  # Python floor preserved


def test_enforce_both_allow(monkeypatch):
    monkeypatch.setenv(od._MODE_ENV, "enforce")
    _patch_opa(monkeypatch, {"allow": True, "verdict": "allow", "reason": "Approved"})
    out = od.opa_delegate_tool_call("Bash", {}, {"result": od._ALLOW, "reason": None})
    assert out["result"] == od._ALLOW


def test_enforce_opa_unreachable_fails_closed(monkeypatch):
    monkeypatch.setenv(od._MODE_ENV, "enforce")
    _patch_opa(monkeypatch, None)  # OPA down
    out = od.opa_delegate_tool_call("Bash", {}, {"result": od._ALLOW, "reason": None})
    assert out["result"] == od._DENY
    assert "failing closed" in str(out["reason"]).lower()


def test_shadow_opa_unreachable_keeps_python(monkeypatch):
    monkeypatch.setenv(od._MODE_ENV, "shadow")
    _patch_opa(monkeypatch, None)
    py = {"result": od._ALLOW, "reason": None}
    out = od.opa_delegate_tool_call("Bash", {}, py)
    assert out is py  # shadow never blocks, even if OPA is down
