"""Unit tests for omnigent.policies.builtins.opa.opa_require_approval.

The builtin is async; tests call it via asyncio.run so no pytest-asyncio is
needed. query_opa_decision is monkeypatched, so no live OPA server is required.
"""

from __future__ import annotations

import asyncio

import omnigent.policies.builtins.opa as opa


def _run(coro):
    return asyncio.run(coro)


def _patch(monkeypatch, decision):
    monkeypatch.setattr(opa, "query_opa_decision", lambda *a, **k: decision)


def _tool_event(name="mcp__github__delete_repository", args=None):
    return {"type": "tool_call", "data": {"name": name, "arguments": args or {}}}


def test_off_returns_allow_and_never_queries(monkeypatch):
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "off")
    calls = []
    monkeypatch.setattr(opa, "query_opa_decision", lambda *a, **k: calls.append(1))
    out = _run(opa.opa_require_approval(_tool_event()))
    assert out == {"result": "ALLOW"}
    assert calls == []  # off must not consult OPA


def test_non_tool_call_allows(monkeypatch):
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "enforce")
    out = _run(opa.opa_require_approval({"type": "llm_request", "data": {}}))
    assert out == {"result": "ALLOW"}


def test_missing_data_allows(monkeypatch):
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "enforce")
    out = _run(opa.opa_require_approval({"type": "tool_call", "data": None}))
    assert out == {"result": "ALLOW"}


def test_missing_tool_name_allows(monkeypatch):
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "enforce")
    out = _run(opa.opa_require_approval({"type": "tool_call", "data": {"arguments": {}}}))
    assert out == {"result": "ALLOW"}


def test_enforce_deny(monkeypatch):
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "enforce")
    _patch(monkeypatch, {"verdict": "deny", "reason": "boundary"})
    out = _run(opa.opa_require_approval(_tool_event()))
    assert out["result"] == "DENY"
    assert out["reason"] == "boundary"


def test_enforce_require_approval_maps_to_ask(monkeypatch):
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "enforce")
    _patch(monkeypatch, {"verdict": "require_approval", "reason": "approve?"})
    out = _run(opa.opa_require_approval(_tool_event("mcp__github__publish_release")))
    assert out["result"] == "ASK"
    assert out["reason"] == "approve?"


def test_enforce_allow(monkeypatch):
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "enforce")
    _patch(monkeypatch, {"verdict": "allow", "reason": "Allowed"})
    out = _run(opa.opa_require_approval(_tool_event("Bash")))
    assert out == {"result": "ALLOW"}


def test_enforce_unknown_verdict_fails_closed(monkeypatch):
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "enforce")
    _patch(monkeypatch, {"verdict": "surprise"})
    out = _run(opa.opa_require_approval(_tool_event()))
    assert out["result"] == "DENY"


def test_enforce_opa_unreachable_fails_closed(monkeypatch):
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "enforce")
    _patch(monkeypatch, None)
    out = _run(opa.opa_require_approval(_tool_event("Bash")))
    assert out["result"] == "DENY"
    assert "failing closed" in str(out["reason"]).lower()


def test_shadow_never_enforces(monkeypatch):
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "shadow")
    _patch(monkeypatch, {"verdict": "deny", "reason": "boundary"})
    out = _run(opa.opa_require_approval(_tool_event()))
    assert out == {"result": "ALLOW"}  # shadow observes only


def test_shadow_opa_unreachable_allows(monkeypatch):
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "shadow")
    _patch(monkeypatch, None)
    out = _run(opa.opa_require_approval(_tool_event("Bash")))
    assert out == {"result": "ALLOW"}
