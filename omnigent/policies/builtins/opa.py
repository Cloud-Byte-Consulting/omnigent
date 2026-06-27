"""Built-in policy that delegates native-tool authorization to the shared OPA bundle.

This is the OE-2 seam, **server-side** (the cleaner home than the original
hook-side `omnigent.opa_delegate` call). The PolicyEngine evaluates this callable
on every ``tool_call`` event; it queries the shared Rego bundle's native-plane
``oe_decision`` rule (the same bundle Sentry queries for MCP ``tools/call``) and
returns a ``PolicyResponse``:

- OPA ``deny``             → ``{"result": "DENY"}``  (irreversible OE boundary)
- OPA ``require_approval`` → ``{"result": "ASK"}``   ← rendered as a **human
  elicitation** by the existing server ASK gate (``evaluate_policy`` →
  ``_hold_native_ask_gate``). This is what closes the "native-plane
  require_approval collapses to deny" gap: the verdict now flows through the same
  gate as a Python ``ASK``, so a governed CLI agent gets an interactive approval
  prompt instead of a hard deny.
- OPA ``allow`` / off / non-tool / shadow → ``{"result": "ALLOW"}`` (no opinion;
  other policies and the harness consent gate still run).

**Staged rollout** via ``OMNIGENT_OPA_DELEGATE_MODE`` (read through
``omnigent.opa_delegate.delegate_mode``):

- ``off`` (default) — OPA is never queried; this policy abstains. Zero change.
- ``shadow`` — OPA is queried and the verdict logged, but never enforced.
- ``enforce`` — DENY/ASK as above; if OPA is unreachable the call **fails closed
  (DENY)**, matching the rest of the native policy hook.

Because the engine composes policies (DENY short-circuits, ASK accumulates), the
old hook-side ``combine_actions`` deny-wins logic is unnecessary — ordering gives
it for free. Attach this policy via a session/agent/`default_policies` spec
(e.g. the Open Engine stack-profile ``guardrails.policies``); the handler path is
``omnigent.policies.builtins.opa.opa_require_approval``.
"""

from __future__ import annotations

import asyncio
import sys

from omnigent.opa_delegate import build_opa_input, delegate_mode, query_opa_decision
from omnigent.policies.schema import PolicyEvent, PolicyResponse

_ALLOW: PolicyResponse = {"result": "ALLOW"}

# Rego tri-state verdict → PolicyResponse result. An unknown verdict fails closed
# to DENY (the bundle should only ever emit these three).
_VERDICT_TO_RESULT = {
    "allow": "ALLOW",
    "deny": "DENY",
    "require_approval": "ASK",
}

_OPA_UNAVAILABLE_REASON = (
    "OPA policy evaluation unavailable; failing closed for this tool call "
    "(opa_delegate enforce mode)."
)


async def opa_require_approval(event: PolicyEvent) -> PolicyResponse:
    """Evaluate a tool call against the shared OPA bundle (native plane).

    Returns ``ASK`` for an OPA ``require_approval`` so the server's existing
    elicitation gate renders a human approval prompt; ``DENY`` for an OPA
    boundary deny; ``ALLOW`` (abstain) otherwise. Mode-gated and fail-closed —
    see the module docstring.
    """
    mode = delegate_mode()
    if mode == "off":
        return _ALLOW
    if event.get("type") != "tool_call":
        return _ALLOW
    data = event.get("data")
    if not isinstance(data, dict):
        return _ALLOW
    tool = data.get("name", "")
    if not tool:
        return _ALLOW
    args = data.get("arguments", {})

    # Subject groups (e.g. Entra OIDs) flow from the authenticated session via the
    # event context (OE-3). With groups present the rego admin carve-out can apply;
    # without them (header-mode auth, or a token carrying no groups claim) it stays
    # [] → is_admin=False → the strict boundary holds for everyone (fail-safe).
    ctx = event.get("context")
    raw_groups = ctx.get("groups") if isinstance(ctx, dict) else None
    groups = raw_groups if isinstance(raw_groups, list) else None
    opa_input = build_opa_input(tool, args, groups=groups)
    # query_opa_decision uses sync httpx; offload it so the event loop is not
    # blocked during the (localhost) OPA round-trip.
    decision = await asyncio.to_thread(query_opa_decision, opa_input)

    if decision is None:
        if mode == "enforce":
            return {"result": "DENY", "reason": _OPA_UNAVAILABLE_REASON}
        print(f"opa[shadow]: OPA unavailable for {tool!r}", file=sys.stderr)
        return _ALLOW

    verdict = str(decision.get("verdict"))
    result = _VERDICT_TO_RESULT.get(verdict, "DENY")  # unknown → fail closed

    if mode == "shadow":
        print(f"opa[shadow]: tool={tool!r} would={result} (verdict={verdict!r})", file=sys.stderr)
        return _ALLOW

    # enforce
    if result == "ALLOW":
        return _ALLOW
    reason = decision.get("reason")
    if result == "ASK":
        return {"result": "ASK", "reason": reason or f"Open Engine boundary: approval required for {tool}."}
    return {"result": "DENY", "reason": reason or f"Open Engine boundary: {tool} denied."}


POLICY_REGISTRY = [
    {
        "handler": "omnigent.policies.builtins.opa.opa_require_approval",
        "kind": "callable",
        "name": "Open Engine OPA Boundaries (opa_delegate)",
        "description": (
            "Delegates native-tool authorization to the shared OPA bundle's "
            "native-plane oe_decision rule: irreversible boundaries (delete/"
            "credentials/billing) -> DENY; ask-first boundaries (publish/email/"
            "deploy) -> ASK (rendered as a human approval prompt); ordinary work "
            "-> ALLOW. Gated by OMNIGENT_OPA_DELEGATE_MODE (off/shadow/enforce); "
            "fail-closed when OPA is unreachable in enforce."
        ),
        "params_schema": None,
    },
]
