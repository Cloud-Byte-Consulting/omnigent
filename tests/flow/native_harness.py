"""Safe helpers for native coding-harness conformance tests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

FLOW_TOOLS = (
    "propose_dag",
    "run_workflow",
    "get_workflow_status",
    "list_workflows",
)
_FLOW_TOOL_IDS = tuple(f"flow-{tool}" for tool in FLOW_TOOLS)
_FLOW_TOOL_PERMISSIONS = tuple(f"flow({tool})" for tool in FLOW_TOOLS)
_SENSITIVE_KEYS = {
    "accesstoken",
    "apikey",
    "approvaltoken",
    "authorization",
    "credential",
    "credentials",
    "flowsigningkey",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "signingkey",
}
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_PROVIDER_KEY = re.compile(r"\bsk-(?:live|test)-[A-Za-z0-9_-]+")
_REDACTED = "[REDACTED]"


class CopilotProtocolError(ValueError):
    """Copilot JSONL did not prove one canonical Flow tool execution."""


@dataclass(frozen=True, slots=True)
class CopilotToolExecution:
    """One correlated Flow MCP execution observed in Copilot JSONL."""

    tool_call_id: str
    server_name: str
    tool_name: str
    model_tool_name: str
    arguments: dict[str, Any] = field(repr=False)
    structured_result: dict[str, Any] = field(repr=False)

    def safe_evidence(self) -> dict[str, Any]:
        """Return an allowlisted, recursively redacted evidence record."""
        return {
            "server": self.server_name,
            "tool": self.tool_name,
            "modelTool": self.model_tool_name,
            "arguments": redact_sensitive(self.arguments),
            "result": redact_sensitive(self.structured_result),
        }


def build_copilot_command(
    prompt: str,
    *,
    executable: str | Path = "copilot",
    mcp_config: str | Path = ".mcp.json",
) -> tuple[str, ...]:
    """Build a noninteractive command exposing and approving only Flow tools."""
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    config = str(mcp_config)
    if not config or config.startswith("@"):
        raise ValueError("mcp_config must be a path without an @ prefix")
    base = (
        str(executable),
        "--prompt",
        prompt,
        "--output-format",
        "json",
        "--stream",
        "off",
        "--no-color",
        "--no-custom-instructions",
        "--no-auto-update",
        "--no-ask-user",
        "--disable-builtin-mcps",
        "--additional-mcp-config",
        f"@{config}",
    )
    available = tuple(
        argument for tool in _FLOW_TOOL_IDS for argument in ("--available-tools", tool)
    )
    permissions = tuple(
        argument for tool in _FLOW_TOOL_PERMISSIONS for argument in ("--allow-tool", tool)
    )
    return (*base, *available, *permissions)


def parse_copilot_tool_execution(
    output: str,
    *,
    expected_tool: str,
) -> CopilotToolExecution:
    """Parse and correlate one successful Flow MCP execution from JSONL."""
    if expected_tool not in FLOW_TOOLS:
        raise ValueError(f"unknown canonical Flow tool: {expected_tool}")
    events = _jsonl_events(output)
    starts = [event for event in events if event.get("type") == "tool.execution_start"]
    if len(starts) != 1:
        raise CopilotProtocolError("expected exactly one tool execution start event")

    start_data = _event_data(starts[0], "tool execution start")
    call_id = _required_string(start_data, "toolCallId", "tool execution start")
    server = _required_string(start_data, "mcpServerName", "tool execution start")
    raw_tool = _required_string(start_data, "mcpToolName", "tool execution start")
    model_tool = _required_string(start_data, "toolName", "tool execution start")
    if server != "flow" or raw_tool != expected_tool:
        raise CopilotProtocolError("tool execution did not target the expected Flow tool")
    if model_tool != f"flow-{expected_tool}":
        raise CopilotProtocolError("Copilot exposed a noncanonical Flow tool name")
    arguments = start_data.get("arguments", {})
    if not isinstance(arguments, dict):
        raise CopilotProtocolError("tool execution arguments must be an object")

    completions = [event for event in events if event.get("type") == "tool.execution_complete"]
    if len(completions) != 1:
        raise CopilotProtocolError("expected exactly one matching tool completion event")
    completion = _event_data(completions[0], "tool execution completion")
    if completion.get("toolCallId") != call_id:
        raise CopilotProtocolError("tool completion does not match its execution start")
    if events.index(completions[0]) <= events.index(starts[0]):
        raise CopilotProtocolError("tool completion preceded its execution start")
    if completion.get("success") is not True:
        raise CopilotProtocolError("Flow tool execution did not succeed")
    result = completion.get("result")
    if not isinstance(result, dict):
        raise CopilotProtocolError("successful tool completion is missing its result")
    structured = _structured_result(result)
    return CopilotToolExecution(
        tool_call_id=call_id,
        server_name=server,
        tool_name=raw_tool,
        model_tool_name=model_tool,
        arguments=arguments,
        structured_result=structured,
    )


def redact_sensitive(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact secret-bearing fields and common inline credentials."""
    if key is not None and _sensitive_key(key):
        return _REDACTED
    if isinstance(value, dict):
        return {item_key: redact_sensitive(item, key=item_key) for item_key, item in value.items()}
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        redacted = _PRIVATE_KEY.sub(_REDACTED, value)
        redacted = _BEARER.sub(f"Bearer {_REDACTED}", redacted)
        return _PROVIDER_KEY.sub(_REDACTED, redacted)
    return value


def _jsonl_events(output: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise CopilotProtocolError(
                f"Copilot output line {line_number} is not valid JSON"
            ) from error
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise CopilotProtocolError(f"Copilot output line {line_number} is not an event")
        events.append(event)
    if not events:
        raise CopilotProtocolError("Copilot produced no JSONL events")
    return events


def _structured_result(result: dict[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if not isinstance(content, str):
        raise CopilotProtocolError("Flow tool result is missing structured content")
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError as error:
        raise CopilotProtocolError("Flow tool result content is not structured JSON") from error
    if isinstance(decoded, dict) and isinstance(decoded.get("structuredContent"), dict):
        return cast(dict[str, Any], decoded["structuredContent"])
    if not isinstance(decoded, dict):
        raise CopilotProtocolError("Flow tool structured result must be an object")
    return cast(dict[str, Any], decoded)


def _event_data(event: dict[str, Any], label: str) -> dict[str, Any]:
    data = event.get("data")
    if not isinstance(data, dict):
        raise CopilotProtocolError(f"{label} data must be an object")
    return data


def _required_string(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise CopilotProtocolError(f"{label} is missing {key}")
    return value


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    suffixes = (
        "accesstoken",
        "apikey",
        "approvaltoken",
        "credential",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "signingkey",
    )
    return normalized in _SENSITIVE_KEYS or normalized.endswith(suffixes)
