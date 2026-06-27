"""Unit tests for omnigent.opa_delegate (the OPA client used by the OPA builtin).

The enforcement decision logic moved to omnigent.policies.builtins.opa
(test_opa_builtin.py); this file covers the pure client helpers.
"""

from __future__ import annotations

import omnigent.opa_delegate as od


def test_parse_native_tool_name_mcp():
    assert od.parse_native_tool_name("mcp__github__delete_repository") == (
        "github",
        "delete_repository",
    )


def test_parse_native_tool_name_mcp_underscored_tool():
    assert od.parse_native_tool_name("mcp__resource_discovery__get_catalog_entry") == (
        "resource_discovery",
        "get_catalog_entry",
    )


def test_parse_native_tool_name_host_tool():
    assert od.parse_native_tool_name("Bash") == ("native", "Bash")


def test_build_opa_input_shape():
    inp = od.build_opa_input("mcp__github__delete_repository", {"name": "x"}, groups=["g1"])
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


def test_delegate_mode_default_off(monkeypatch):
    monkeypatch.delenv(od._MODE_ENV, raising=False)
    assert od.delegate_mode() == "off"


def test_delegate_mode_unknown_is_off(monkeypatch):
    monkeypatch.setenv(od._MODE_ENV, "ENFROCE")  # typo
    assert od.delegate_mode() == "off"


def test_delegate_mode_valid(monkeypatch):
    for m in ("off", "shadow", "enforce"):
        monkeypatch.setenv(od._MODE_ENV, m.upper())  # case-insensitive
        assert od.delegate_mode() == m


def test_opa_decision_url_from_env(monkeypatch):
    monkeypatch.setenv(od._OPA_URL_ENV, "http://opa.internal:8181/")
    assert od.opa_decision_url() == "http://opa.internal:8181/v1/data/mcp/auth/oe_decision"
