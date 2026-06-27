"""Unit tests for omnigent.policies.builtins.opa.opa_require_approval.

The builtin is async; tests call it via asyncio.run so no pytest-asyncio is
needed. query_opa_decision is monkeypatched, so no live OPA server is required.
"""

from __future__ import annotations

import asyncio
import json

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


# ── Phase 5: contract-A authz decision log (plane="native") ──────────────────


def test_enforce_writes_audit_log_line(monkeypatch, tmp_path):
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "enforce")
    monkeypatch.setenv("OE_AUDIT_LOG", str(log))
    _patch(monkeypatch, {"verdict": "require_approval", "reason": "approve?"})
    ev = {
        "type": "tool_call",
        "data": {"name": "mcp__github__delete_repository", "arguments": {}},
        "context": {"session_id": "conv_abc123", "subject_id": "entra-oid-123"},
    }
    out = _run(opa.opa_require_approval(ev))
    assert out["result"] == "ASK"

    line = json.loads(log.read_text().strip())
    assert line["plane"] == "native"
    assert line["session_id"] == "conv_abc123"
    assert line["subject_id"] == "entra-oid-123"
    assert line["verdict"] == "require_approval"  # ASK → require_approval
    assert line["server_name"] == "github"
    assert line["tool_name"] == "delete_repository"
    assert line["ts"]  # RFC3339 timestamp present


def test_enforce_opa_unreachable_writes_deny_audit_line(monkeypatch, tmp_path):
    # An OPA-unavailable enforce denial must still leave an authz trace (no silent
    # audit gap), not just fail closed.
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "enforce")
    monkeypatch.setenv("OE_AUDIT_LOG", str(log))
    _patch(monkeypatch, None)  # OPA unreachable
    ev = {
        "type": "tool_call",
        "data": {"name": "mcp__github__delete_repository", "arguments": {}},
        "context": {"session_id": "conv_x", "subject_id": "subj_x"},
    }
    out = _run(opa.opa_require_approval(ev))
    assert out["result"] == "DENY"
    line = json.loads(log.read_text().strip())
    assert line["verdict"] == "deny"
    assert line["session_id"] == "conv_x"
    assert "unavailable" in line["reason"].lower()


def test_no_audit_env_writes_nothing(monkeypatch, tmp_path):
    log = tmp_path / "audit.jsonl"
    monkeypatch.delenv("OE_AUDIT_LOG", raising=False)
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "enforce")
    _patch(monkeypatch, {"verdict": "deny", "reason": "boundary"})
    _run(opa.opa_require_approval(_tool_event()))
    assert not log.exists()  # opt-in: unset env => no emit


def test_shadow_does_not_write_audit_log(monkeypatch, tmp_path):
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("OE_AUDIT_LOG", str(log))
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "shadow")
    _patch(monkeypatch, {"verdict": "deny", "reason": "boundary"})
    _run(opa.opa_require_approval(_tool_event()))
    assert not log.exists()  # shadow observes only; logs real (enforced) decisions only


# ── OE-3: subject groups forwarding (admin carve-out) ────────────────────────


def _capture_input(monkeypatch):
    """Patch query_opa_decision to capture the opa_input it receives."""
    box = {}

    def cap(inp, **kw):
        box["input"] = inp
        return {"verdict": "allow"}

    monkeypatch.setattr(opa, "query_opa_decision", cap)
    return box


def test_groups_from_event_context_are_forwarded(monkeypatch):
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "enforce")
    box = _capture_input(monkeypatch)
    ev = {
        "type": "tool_call",
        "data": {"name": "mcp__github__delete_repository", "arguments": {}},
        "context": {"groups": ["admin-oid", "other"]},
    }
    _run(opa.opa_require_approval(ev))
    assert box["input"]["groups"] == ["admin-oid", "other"]


def test_no_context_groups_is_empty(monkeypatch):
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "enforce")
    box = _capture_input(monkeypatch)
    _run(opa.opa_require_approval(_tool_event()))  # no context
    assert box["input"]["groups"] == []  # fail-safe: no groups → strict


def test_non_list_groups_is_empty(monkeypatch):
    monkeypatch.setenv("OMNIGENT_OPA_DELEGATE_MODE", "enforce")
    box = _capture_input(monkeypatch)
    ev = {"type": "tool_call", "data": {"name": "Bash", "arguments": {}}, "context": {"groups": "admin"}}
    _run(opa.opa_require_approval(ev))
    assert box["input"]["groups"] == []  # malformed groups → strict, never trusted
