"""opa_delegate — evaluate native tool calls against the shared OPA bundle.

This is the OE-2 seam (``docs/architecture/open-engine-integration-plan.md`` in
agentic-harness): native host tools (``Bash`` / ``Write`` / ``Edit`` / …) and
connector-MCP tools (``mcp__github__*``), gated by the ``PreToolUse`` hook in
:mod:`omnigent.native_policy_hook`, are evaluated against the *same* Rego bundle
Sentry queries for MCP ``tools/call``, via the bundle's native-plane
``data.mcp.auth.oe_decision`` rule (default ALLOW + OE boundaries only; host
tools have no MCP allow-list entry, so they must not inherit the gateway
``decision``'s default-deny). One bundle,
two enforcement points — native and MCP planes return one deterministic
tri-state verdict (``allow | deny | require_approval``).

**Staged rollout** via ``OMNIGENT_OPA_DELEGATE_MODE``:

- ``off`` (default) — OPA is not consulted; the Python policy verdict stands.
  Zero behaviour change.
- ``shadow`` — OPA is evaluated and the verdict is logged next to the Python
  verdict, but **not enforced**. Use this to collect real-world parity data
  before trusting OPA (plan risk 1; the Wave-0 parity spike de-risked only the
  verdict *mapping*, not full builtin parity).
- ``enforce`` — **deny-wins**: the stricter of {Python, OPA} is enforced, so the
  Open Engine boundary denies fire on native tools while the Python policy stays
  a floor. If OPA is unreachable in this mode the call **fails closed (DENY)**,
  matching :func:`omnigent.native_policy_hook.fail_closed_hook_output`.

Promote ``off → shadow → enforce`` as parity proves out. The verdict vocabulary
matches the Python hook (``POLICY_ACTION_*``), so the combined result feeds
straight into :func:`omnigent.native_policy_hook.evaluation_response_to_hook_output`.
"""

from __future__ import annotations

import os
import sys

import httpx

# POLICY_ACTION_* verdicts shared with omnigent.native_policy_hook. Kept as bare
# strings (not an import) so this module stays dependency-light like the hook.
_ALLOW = "POLICY_ACTION_ALLOW"
_DENY = "POLICY_ACTION_DENY"
_ASK = "POLICY_ACTION_ASK"
_UNSPECIFIED = "POLICY_ACTION_UNSPECIFIED"

# Tri-state Rego verdict (Agentic-Sentry/mcp-policies/policies/mcp_auth.rego)
# → POLICY_ACTION_*. require_approval maps to ASK; the native hook / server then
# renders the human approval (an Omnigent ASK), exactly as for a Python ASK.
_VERDICT_TO_ACTION = {
    "allow": _ALLOW,
    "deny": _DENY,
    "require_approval": _ASK,
}

# Deny-wins precedence for combining the Python and OPA verdicts. DENY beats ASK
# beats ALLOW; an unspecified/empty action is treated as "no opinion" (ALLOW).
_ACTION_RANK = {_ALLOW: 0, _UNSPECIFIED: 0, _ASK: 1, _DENY: 2}

_MODE_ENV = "OMNIGENT_OPA_DELEGATE_MODE"
_OPA_URL_ENV = "OMNIGENT_OPA_URL"
_DEFAULT_OPA_URL = "http://127.0.0.1:8181"
# OPA REST path for the native-plane OE-boundary decision. ``oe_decision``
# defaults to ALLOW and denies/asks only on the shared OE boundary rules — the
# native hook must not inherit the gateway ``decision``'s default-deny baseline
# (a host ``Bash`` call has no MCP server allow-list entry). OPA wraps the rule
# result under ``result``: POST {"input": …} → {"result": {verdict, reason}}.
_DECISION_PATH = "/v1/data/mcp/auth/oe_decision"
_OPA_TIMEOUT_S = 5.0

_VALID_MODES = ("off", "shadow", "enforce")

# Reason surfaced when OPA cannot be reached in ``enforce`` mode (fail closed).
_OPA_UNAVAILABLE_REASON = (
    "OPA policy evaluation unavailable; failing closed for this tool call "
    "(opa_delegate enforce mode)."
)


def delegate_mode() -> str:
    """Return the configured opa_delegate mode (``off`` | ``shadow`` | ``enforce``).

    Unknown / unset values fall back to ``off`` so a typo can never silently
    flip enforcement on (or, worse, off when the operator meant on — an unknown
    value is conservative either way because ``off`` changes nothing).
    """
    mode = os.environ.get(_MODE_ENV, "off").strip().lower()
    return mode if mode in _VALID_MODES else "off"


def opa_decision_url(base_url: str | None = None) -> str:
    """Build the absolute OPA decision URL from a base (env ``OMNIGENT_OPA_URL``)."""
    base = (base_url or os.environ.get(_OPA_URL_ENV) or _DEFAULT_OPA_URL).rstrip("/")
    return base + _DECISION_PATH


def parse_native_tool_name(tool_name: str) -> tuple[str, str]:
    """Split a native/connector tool name into ``(server_name, tool_name)``.

    Connector-MCP tools surface as ``mcp__<server>__<tool>`` (the tool segment
    may itself contain single underscores, e.g.
    ``mcp__github__delete_repository``). Host-native tools (``Bash``, ``Write``)
    have no server, so they are reported under the synthetic server ``"native"``.

    :returns: ``("github", "delete_repository")`` for an MCP tool,
        ``("native", "Bash")`` for a host tool.
    """
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        if len(parts) >= 3:
            return parts[1], "__".join(parts[2:])
    return "native", tool_name


def build_opa_input(
    tool_name: str,
    arguments: object,
    *,
    groups: list[str] | None = None,
) -> dict[str, object]:
    """Shape a tool call into the OPA input schema ``mcp_auth.rego`` reads.

    The rego's ``oe_decision`` rule reads ``input.tool_name`` (token-matched for
    the OE boundaries) and ``input.groups`` (for the admin carve-out);
    ``server_name`` / ``arguments`` are included for parity with the gateway
    input shape. ``groups`` is empty until the subject/Entra binding lands
    (OE-3); with no groups the rego applies the strict Open Engine boundary
    (e.g. delete → deny) to *everyone*, including would-be admins — fail-safe
    until the caller's groups are known.

    :param tool_name: The harness-supplied tool name (``mcp__server__tool`` or a
        host tool like ``Bash``); the server segment is split out automatically.
    :param arguments: The tool input/arguments dict (passed through as-is).
    :param groups: Subject security groups, when known (default: none).
    """
    server_name, bare_tool = parse_native_tool_name(tool_name)
    return {
        "server_name": server_name,
        "tool_name": bare_tool,
        "arguments": arguments if arguments is not None else {},
        "groups": groups or [],
    }


def query_opa_decision(
    opa_input: dict[str, object],
    *,
    opa_url: str | None = None,
    timeout: float = _OPA_TIMEOUT_S,
) -> dict[str, object] | None:
    """POST the input to OPA's decision endpoint; return the decision or ``None``.

    Returns the parsed ``{allow, verdict, reason}`` decision object on a 2xx with
    a well-formed body, or ``None`` on any error (unreachable, non-2xx, missing
    ``result``). The caller decides what ``None`` means per mode (shadow: skip
    the comparison; enforce: fail closed).
    """
    url = opa_decision_url(opa_url)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json={"input": opa_input})
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:  # noqa: BLE001
        # ANY failure to obtain a verdict returns None so the caller fails
        # closed in enforce mode. Catch broadly on purpose: httpx.InvalidURL
        # (a misconfigured OMNIGENT_OPA_URL) is NOT an HTTPError subclass and
        # must not escape and crash the PreToolUse hook (which could fail open).
        print(f"opa_delegate: OPA query failed: {exc}", file=sys.stderr)
        return None
    result = body.get("result") if isinstance(body, dict) else None
    # An empty OPA result ({}) means the decision document did not evaluate —
    # treat as a failed query rather than a silent allow.
    if not isinstance(result, dict) or "verdict" not in result:
        print(
            f"opa_delegate: OPA returned no decision for {opa_input.get('tool_name')!r}",
            file=sys.stderr,
        )
        return None
    return result


def opa_verdict_to_action(verdict: object) -> str:
    """Map a tri-state Rego ``verdict`` to a ``POLICY_ACTION_*`` string.

    An unknown verdict fails closed to ``DENY`` — the bundle should only ever
    emit the three known values, so anything else is a contract violation we do
    not want to wave through.
    """
    return _VERDICT_TO_ACTION.get(str(verdict), _DENY)


def combine_actions(python_action: object, opa_action: object) -> str:
    """Combine two ``POLICY_ACTION_*`` verdicts, deny-wins (DENY > ASK > ALLOW)."""
    py = _ACTION_RANK.get(str(python_action), 0)
    op = _ACTION_RANK.get(str(opa_action), 0)
    return str(python_action) if py >= op else str(opa_action)


def opa_delegate_tool_call(
    tool_name: str,
    arguments: object,
    python_response: dict[str, object],
    *,
    groups: list[str] | None = None,
    opa_url: str | None = None,
) -> dict[str, object]:
    """Return the effective tool-call verdict after OPA delegation, per mode.

    Call this for a ``PreToolUse`` / ``PHASE_TOOL_CALL`` event right after the
    Python ``EvaluationResponse`` is obtained and before
    :func:`omnigent.native_policy_hook.evaluation_response_to_hook_output`. The
    returned dict is the same ``{"result": POLICY_ACTION_*, "reason": …}`` shape,
    so it drops straight into the existing hook-output mapping.

    - ``off``: returns ``python_response`` unchanged.
    - ``shadow``: queries OPA, logs ``python vs opa``, returns ``python_response``.
    - ``enforce``: returns deny-wins({python, opa}); if OPA is unreachable,
      returns a fail-closed ``DENY``.

    :param tool_name: Harness tool name (``mcp__server__tool`` or host tool).
    :param arguments: Tool input/arguments.
    :param python_response: The ``EvaluationResponse`` from the Python policy
        server (``{"result": POLICY_ACTION_*, "reason": …}``).
    :param groups: Subject groups, when known (default none → strict boundary).
    :param opa_url: Override the OPA base URL (default env / localhost).
    """
    mode = delegate_mode()
    if mode == "off":
        return python_response

    opa_input = build_opa_input(tool_name, arguments, groups=groups)
    decision = query_opa_decision(opa_input, opa_url=opa_url)

    python_action = python_response.get("result", _UNSPECIFIED)

    if decision is None:
        if mode == "enforce":
            # Fail closed: OPA was asked to govern and could not answer.
            return {"result": _DENY, "reason": _OPA_UNAVAILABLE_REASON}
        # shadow: nothing to compare; leave the Python verdict in place.
        print(
            f"opa_delegate[shadow]: OPA unavailable for {tool_name!r}; "
            f"python={python_action}",
            file=sys.stderr,
        )
        return python_response

    opa_action = opa_verdict_to_action(decision.get("verdict"))

    if mode == "shadow":
        agree = "match" if str(opa_action) == str(python_action) else "DIVERGE"
        print(
            f"opa_delegate[shadow] {agree}: tool={tool_name!r} "
            f"python={python_action} opa={opa_action} "
            f"(opa_verdict={decision.get('verdict')!r})",
            file=sys.stderr,
        )
        return python_response

    # enforce: deny-wins. Carry the reason from whichever side is being enforced.
    combined = combine_actions(python_action, opa_action)
    if combined == opa_action and combined != python_action:
        reason = decision.get("reason") or "Denied by Open Engine policy (OPA)"
    else:
        reason = python_response.get("reason")
    return {"result": combined, "reason": reason}


if __name__ == "__main__":
    # Smoke self-check (no OPA needed): mapping, parsing, combine, mode default.
    assert parse_native_tool_name("mcp__github__delete_repository") == (
        "github",
        "delete_repository",
    )
    assert parse_native_tool_name("Bash") == ("native", "Bash")
    assert opa_verdict_to_action("require_approval") == _ASK
    assert opa_verdict_to_action("deny") == _DENY
    assert opa_verdict_to_action("nonsense") == _DENY  # unknown → fail closed
    assert combine_actions(_ALLOW, _DENY) == _DENY  # deny wins
    assert combine_actions(_ASK, _ALLOW) == _ASK
    assert combine_actions(_DENY, _ASK) == _DENY
    assert delegate_mode() in _VALID_MODES
    # off mode returns the Python response untouched (no OPA call).
    os.environ[_MODE_ENV] = "off"
    py = {"result": _ALLOW, "reason": None}
    assert opa_delegate_tool_call("Bash", {}, py) is py
    print("opa_delegate self-check passed")
