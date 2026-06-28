"""opa_delegate — OPA client for the Open Engine native-plane boundary check.

The OE-2 seam. Shapes a tool call into the OPA input schema and queries the
shared Rego bundle's native-plane ``data.mcp.auth.oe_decision`` rule (the same
bundle Sentry queries for MCP ``tools/call``; ``oe_decision`` defaults to ALLOW
and denies/asks only on the OE boundary rules, so host tools like ``Bash`` — which
have no MCP allow-list entry — are not blanket-denied by the gateway's default-deny).

This module is the **client only**. The verdict is turned into an enforcement
decision by the server-side policy builtin
:mod:`omnigent.policies.builtins.opa` (which the PolicyEngine evaluates and the
existing ASK gate renders). The builtin replaced the original hook-side
delegation, so the deny-wins ``combine_actions`` / ``opa_delegate_tool_call``
logic lives in the engine's policy composition now, not here.

**Staged rollout** via ``OMNIGENT_OPA_DELEGATE_MODE`` (read by the builtin
through :func:`delegate_mode`): ``off`` (default — never queried) → ``shadow``
(queried + logged, not enforced) → ``enforce`` (DENY/ASK; OPA-unreachable fails
closed). ``OMNIGENT_OPA_URL`` overrides the OPA base URL (default localhost:8181).
"""

from __future__ import annotations

import os
import sys

import httpx

_MODE_ENV = "OMNIGENT_OPA_DELEGATE_MODE"
_OPA_URL_ENV = "OMNIGENT_OPA_URL"
_DEFAULT_OPA_URL = "http://127.0.0.1:8181"
# OPA REST path for the native-plane OE-boundary decision. ``oe_decision``
# defaults to ALLOW and denies/asks only on the shared OE boundary rules. OPA
# wraps the rule result under ``result``: POST {"input": …} → {"result": {verdict, reason}}.
_DECISION_PATH = "/v1/data/mcp/auth/oe_decision"
_OPA_TIMEOUT_S = 5.0

_VALID_MODES = ("off", "shadow", "enforce")


def delegate_mode() -> str:
    """Return the configured opa_delegate mode (``off`` | ``shadow`` | ``enforce``).

    Unknown / unset values fall back to ``off`` so a typo can never silently
    flip enforcement on — an unknown value is conservative because ``off``
    changes nothing.
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
    may itself contain single underscores, e.g. ``mcp__github__delete_repository``).
    Host-native tools (``Bash``, ``Write``) have no server, so they are reported
    under the synthetic server ``"native"``.

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

    Returns the parsed ``{verdict, reason, …}`` decision object on a 2xx with a
    well-formed body, or ``None`` on any error (unreachable, non-2xx, bad JSON,
    missing ``result``/``verdict``). The caller decides what ``None`` means per
    mode (shadow: skip; enforce: fail closed).
    """
    url = opa_decision_url(opa_url)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json={"input": opa_input})
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:  # noqa: BLE001
        # ANY failure to obtain a verdict returns None so the caller fails closed
        # in enforce mode. Catch broadly on purpose: httpx.InvalidURL (a
        # misconfigured OMNIGENT_OPA_URL) is NOT an HTTPError subclass and must
        # not escape and crash the policy evaluation.
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


if __name__ == "__main__":
    # Smoke self-check (no OPA needed): parsing, input shaping, mode default.
    assert parse_native_tool_name("mcp__github__delete_repository") == (
        "github",
        "delete_repository",
    )
    assert parse_native_tool_name("Bash") == ("native", "Bash")
    assert build_opa_input("Bash", None)["server_name"] == "native"
    assert build_opa_input("Bash", None)["arguments"] == {}
    assert delegate_mode() in _VALID_MODES
    assert opa_decision_url("http://x:8181/").endswith("/v1/data/mcp/auth/oe_decision")
    print("opa_delegate self-check passed")
