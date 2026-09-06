"""Safe helpers for native coding-harness conformance tests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import tomllib

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
_CODEX_CONFIG = Path(__file__).parents[2] / "docs" / "flow" / "harnesses" / "codex.toml"


class CopilotProtocolError(ValueError):
    """Copilot JSONL did not prove one canonical Flow tool execution."""


class CodexProtocolError(ValueError):
    """Codex JSONL did not prove one canonical Flow tool execution."""


class OpenCodeProtocolError(ValueError):
    """OpenCode JSONL did not prove one canonical Flow tool execution."""


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


@dataclass(frozen=True, slots=True)
class CodexToolExecution:
    """One correlated Flow MCP execution observed in Codex JSONL."""

    item_id: str
    server_name: str
    tool_name: str
    arguments: dict[str, Any] = field(repr=False)
    structured_result: dict[str, Any] = field(repr=False)

    def safe_evidence(self) -> dict[str, Any]:
        """Return an allowlisted, recursively redacted evidence record."""
        return {
            "server": self.server_name,
            "tool": self.tool_name,
            "arguments": redact_sensitive(self.arguments),
            "result": redact_sensitive(self.structured_result),
        }


@dataclass(frozen=True, slots=True)
class OpenCodeToolExecution:
    """One correlated Flow MCP execution observed in OpenCode JSONL."""

    call_id: str
    session_id: str
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


def build_codex_command(
    expected_tool: str,
    *,
    executable: str | Path = "codex",
    flow_entrypoint: str | Path = "flow-mcp",
    mcp_config: str | Path = _CODEX_CONFIG,
) -> tuple[str, ...]:
    """Build a hardened Codex exec command scoped to one Flow tool."""
    if expected_tool not in FLOW_TOOLS:
        raise ValueError(f"unknown canonical Flow tool: {expected_tool}")
    entrypoint = str(flow_entrypoint)
    if not entrypoint:
        raise ValueError("flow_entrypoint must not be empty")
    flow = _codex_flow_configuration(Path(mcp_config))
    forwarded = [
        "FLOW_MODE",
        "FLOW_ACTOR",
        "FLOW_SIGNING_KEY",
        "FLOW_APPROVAL_DB",
        "FLOW_APPROVAL_TTL_SECONDS",
        "FLOW_DAPR_HEALTH_TIMEOUT_SECONDS",
        "DAPR_GRPC_PORT",
        "DAPR_HTTP_PORT",
        "PYTHONNOUSERSITE",
    ]
    overrides = (
        'approval_policy="never"',
        'web_search="disabled"',
        "project_doc_max_bytes=0",
        "features.shell_tool=false",
        "features.apps=false",
        "features.goals=false",
        "features.hooks=false",
        "features.multi_agent=false",
        "features.memories=false",
        "features.remote_plugin=false",
        'shell_environment_policy.inherit="none"',
        "shell_environment_policy.ignore_default_excludes=false",
        f"mcp_servers.flow.command={json.dumps(entrypoint)}",
        f"mcp_servers.flow.args={json.dumps(flow['args'])}",
        f"mcp_servers.flow.enabled={str(flow['enabled']).lower()}",
        f"mcp_servers.flow.required={str(flow['required']).lower()}",
        f"mcp_servers.flow.startup_timeout_sec={flow['startup_timeout_sec']}",
        f"mcp_servers.flow.tool_timeout_sec={flow['tool_timeout_sec']}",
        f"mcp_servers.flow.enabled_tools={json.dumps([expected_tool])}",
        'mcp_servers.flow.default_tools_approval_mode="approve"',
        f"mcp_servers.flow.env_vars={json.dumps(forwarded)}",
        f"mcp_servers.flow.env={{FLOW_LOG_LEVEL={json.dumps(flow['env']['FLOW_LOG_LEVEL'])}}}",
    )
    command = (
        str(executable),
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--strict-config",
        "--sandbox",
        "read-only",
        "--json",
        "--color",
        "never",
    )
    configured = tuple(value for override in overrides for value in ("-c", override))
    return (*command, *configured, "-")


def build_opencode_command(
    *,
    expected_tool: str,
    config: Path,
    prompt_file: Path,
    executable: str | Path = "opencode",
    model: str = "opencode/deepseek-v4-flash-free",
) -> tuple[str, ...]:
    """Build a fresh, JSON-mode OpenCode run with its real prompt in a private file."""
    if expected_tool not in FLOW_TOOLS:
        raise ValueError(f"unknown canonical Flow tool: {expected_tool}")
    if not str(executable):
        raise ValueError("executable must not be empty")
    if not model.strip() or model.startswith("-"):
        raise ValueError("model must be a nonempty provider/model identifier")

    config_path = config.resolve()
    if config.name != "opencode.json" or config.is_symlink() or not config.is_file():
        raise ValueError("config must be a regular opencode.json file")
    prompt_path = prompt_file.resolve()
    if prompt_file.is_symlink() or not prompt_file.is_file():
        raise ValueError("prompt_file must be a regular file")
    if prompt_file.stat().st_mode & 0o777 != 0o600:
        raise ValueError("prompt_file must have mode 0600")
    if not prompt_file.read_text(encoding="utf-8").strip():
        raise ValueError("prompt_file must not be empty")

    fixed_prompt = (
        f"Execute only flow_{expected_tool} using the attached instructions. "
        "Do not use any other tool."
    )
    # OpenCode 1.2.10 declares --file as an array option, so it must remain last:
    # a positional message placed after it is greedily interpreted as another file.
    return (
        str(executable),
        "run",
        fixed_prompt,
        "--format",
        "json",
        "--model",
        model,
        "--agent",
        "build",
        "--title",
        f"flow-{expected_tool}-conformance",
        "--dir",
        str(config_path.parent),
        "--file",
        str(prompt_path),
    )


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


def parse_codex_tool_execution(
    output: str,
    *,
    expected_tool: str,
) -> CodexToolExecution:
    """Parse and correlate one successful Flow MCP execution from Codex JSONL."""
    if expected_tool not in FLOW_TOOLS:
        raise ValueError(f"unknown canonical Flow tool: {expected_tool}")
    events = _codex_jsonl_events(output)
    if any(event.get("type") in {"error", "turn.failed"} for event in events):
        raise CodexProtocolError("Codex turn failed")
    thread_starts = [
        index for index, event in enumerate(events) if event.get("type") == "thread.started"
    ]
    turn_starts = [
        index for index, event in enumerate(events) if event.get("type") == "turn.started"
    ]
    turn_completions = [
        index for index, event in enumerate(events) if event.get("type") == "turn.completed"
    ]
    if len(thread_starts) != 1 or len(turn_starts) != 1 or len(turn_completions) != 1:
        raise CodexProtocolError("expected one coherent Codex thread and turn lifecycle")
    items = [
        (index, event.get("type"), event.get("item"))
        for index, event in enumerate(events)
        if event.get("type") in {"item.started", "item.updated", "item.completed"}
    ]
    if any(not isinstance(item, dict) for _index, _event_type, item in items):
        raise CodexProtocolError("Codex item event must contain an item object")
    forbidden = [
        item
        for _index, _event_type, item in items
        if isinstance(item, dict)
        and item.get("type") not in {"mcp_tool_call", "agent_message", "reasoning"}
    ]
    if forbidden:
        raise CodexProtocolError("Codex performed an unrelated action")
    starts = [
        (index, item)
        for index, event_type, item in items
        if event_type == "item.started"
        and isinstance(item, dict)
        and item.get("type") == "mcp_tool_call"
    ]
    if len(starts) != 1:
        raise CodexProtocolError("expected exactly one MCP tool start event")
    completions = [
        (index, item)
        for index, event_type, item in items
        if event_type == "item.completed"
        and isinstance(item, dict)
        and item.get("type") == "mcp_tool_call"
    ]
    if len(completions) != 1:
        raise CodexProtocolError("expected exactly one MCP tool completion event")
    start_index, start = starts[0]
    completion_index, completion = completions[0]
    item_id = _codex_required_string(start, "id", "MCP tool start")
    if completion.get("id") != item_id:
        raise CodexProtocolError("MCP tool completion does not match its start")
    if completion_index <= start_index:
        raise CodexProtocolError("MCP tool completion preceded its start")
    if not (
        thread_starts[0] < turn_starts[0] < start_index < completion_index < turn_completions[0]
    ):
        raise CodexProtocolError("Codex MCP events violate the thread and turn lifecycle")
    for item in (start, completion):
        if item.get("server") != "flow" or item.get("tool") != expected_tool:
            raise CodexProtocolError("MCP execution did not target the expected Flow tool")
    arguments = start.get("arguments")
    if not isinstance(arguments, dict):
        raise CodexProtocolError("MCP tool arguments must be an object")
    if not json_values_equal(completion.get("arguments"), arguments):
        raise CodexProtocolError("MCP tool completion arguments do not match its start")
    if (
        start.get("status") != "in_progress"
        or start.get("result") is not None
        or start.get("error") is not None
    ):
        raise CodexProtocolError("Flow MCP tool start event is malformed")
    if completion.get("status") != "completed" or completion.get("error") is not None:
        raise CodexProtocolError("Flow MCP tool did not complete successfully")
    result = completion.get("result")
    if not isinstance(result, dict):
        raise CodexProtocolError("completed Flow MCP tool is missing its result")
    return CodexToolExecution(
        item_id=item_id,
        server_name="flow",
        tool_name=expected_tool,
        arguments=arguments,
        structured_result=_codex_structured_result(result),
    )


def parse_opencode_tool_execution(
    output: str,
    *,
    expected_tool: str,
) -> OpenCodeToolExecution:
    """Parse one successful, lifecycle-correlated Flow call from OpenCode JSONL."""
    if expected_tool not in FLOW_TOOLS:
        raise ValueError(f"unknown canonical Flow tool: {expected_tool}")
    events = _opencode_jsonl_events(output)
    allowed_types = {"step_start", "tool_use", "step_finish", "text", "reasoning"}
    unsupported = [event.get("type") for event in events if event.get("type") not in allowed_types]
    if unsupported:
        raise OpenCodeProtocolError("OpenCode emitted an error or unsupported event")

    normalized = [
        (index, event, _opencode_event_part(event)) for index, event in enumerate(events)
    ]
    session_ids = {event["sessionID"] for _index, event, _part in normalized}
    if len(session_ids) != 1:
        raise OpenCodeProtocolError("OpenCode events span multiple sessions")
    session_id = cast(str, next(iter(session_ids)))
    _validate_opencode_lifecycle(normalized)

    tool_events = [item for item in normalized if item[1]["type"] == "tool_use"]
    if len(tool_events) != 1:
        raise OpenCodeProtocolError("expected exactly one OpenCode tool execution")
    tool_index, _tool_event, part = tool_events[0]
    model_tool = _opencode_required_string(part, "tool", "tool execution")
    if model_tool != f"flow_{expected_tool}":
        raise OpenCodeProtocolError("tool execution did not target the expected Flow tool")
    call_id = _opencode_required_string(part, "callID", "tool execution")
    message_id = _opencode_required_string(part, "messageID", "tool execution")

    starts = [
        (index, candidate)
        for index, event, candidate in normalized
        if event["type"] == "step_start"
        and index < tool_index
        and candidate.get("messageID") == message_id
    ]
    finishes = [
        (index, candidate)
        for index, event, candidate in normalized
        if event["type"] == "step_finish"
        and index > tool_index
        and candidate.get("messageID") == message_id
    ]
    if not starts or not finishes:
        raise OpenCodeProtocolError("tool execution is outside a coherent OpenCode step")
    finish = finishes[0][1]
    if finish.get("reason") != "tool-calls":
        raise OpenCodeProtocolError("tool execution step did not finish with a tool call")

    state = part.get("state")
    if not isinstance(state, dict):
        raise OpenCodeProtocolError("tool execution state must be an object")
    if state.get("status") != "completed" or "error" in state:
        raise OpenCodeProtocolError("Flow tool execution did not complete successfully")
    arguments = state.get("input")
    if not isinstance(arguments, dict):
        raise OpenCodeProtocolError("Flow tool arguments must be an object")
    metadata = state.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("truncated") is not False:
        raise OpenCodeProtocolError("Flow tool output is missing or truncated")
    if state.get("attachments"):
        raise OpenCodeProtocolError("Flow tool output contains unexpected attachments")
    _opencode_tool_time(state.get("time"))
    raw_result = state.get("output")
    if not isinstance(raw_result, str) or not raw_result.strip():
        raise OpenCodeProtocolError("Flow tool output must be nonempty structured JSON")

    return OpenCodeToolExecution(
        call_id=call_id,
        session_id=session_id,
        server_name="flow",
        tool_name=expected_tool,
        model_tool_name=model_tool,
        arguments=arguments,
        structured_result=_opencode_structured_result(raw_result),
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


def json_values_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool/int equality coercion."""
    try:
        return json.dumps(left, sort_keys=True, separators=(",", ":")) == json.dumps(
            right,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return False


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


def _codex_jsonl_events(output: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise CodexProtocolError(
                f"Codex output line {line_number} is not valid JSON"
            ) from error
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise CodexProtocolError(f"Codex output line {line_number} is not an event")
        events.append(event)
    if not events:
        raise CodexProtocolError("Codex produced no JSONL events")
    return events


def _opencode_jsonl_events(output: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as error:
            raise OpenCodeProtocolError(
                f"OpenCode output line {line_number} is not valid JSON"
            ) from error
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise OpenCodeProtocolError(f"OpenCode output line {line_number} is not an event")
        timestamp = event.get("timestamp")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
            raise OpenCodeProtocolError("OpenCode event timestamp must be a positive integer")
        session_id = event.get("sessionID")
        if not isinstance(session_id, str) or not session_id:
            raise OpenCodeProtocolError("OpenCode event is missing sessionID")
        events.append(event)
    if not events:
        raise OpenCodeProtocolError("OpenCode produced no JSONL events")
    return events


def _opencode_event_part(event: dict[str, Any]) -> dict[str, Any]:
    part = event.get("part")
    if not isinstance(part, dict):
        raise OpenCodeProtocolError("OpenCode event part must be an object")
    expected_part_types = {
        "step_start": "step-start",
        "tool_use": "tool",
        "step_finish": "step-finish",
        "text": "text",
        "reasoning": "reasoning",
    }
    expected_part_type = expected_part_types.get(cast(str, event.get("type")))
    if expected_part_type is None or part.get("type") != expected_part_type:
        raise OpenCodeProtocolError("OpenCode event and part types do not match")
    if part.get("sessionID") != event.get("sessionID"):
        raise OpenCodeProtocolError("OpenCode event and part sessions do not match")
    _opencode_required_string(part, "id", "event part")
    _opencode_required_string(part, "messageID", "event part")
    return part


def _opencode_required_string(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise OpenCodeProtocolError(f"{label} is missing {key}")
    return value


def _validate_opencode_lifecycle(
    events: list[tuple[int, dict[str, Any], dict[str, Any]]],
) -> None:
    starts: dict[str, list[int]] = {}
    finishes: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, event, part in events:
        message_id = cast(str, part["messageID"])
        if event["type"] == "step_start":
            starts.setdefault(message_id, []).append(index)
        elif event["type"] == "step_finish":
            if part.get("reason") not in {"tool-calls", "stop"}:
                raise OpenCodeProtocolError("OpenCode step has a non-success terminal reason")
            finishes.setdefault(message_id, []).append((index, part))

    if not starts or not finishes:
        raise OpenCodeProtocolError("OpenCode output is missing its step lifecycle")
    for message_id in starts.keys() | finishes.keys():
        message_starts = starts.get(message_id, [])
        message_finishes = finishes.get(message_id, [])
        if len(message_starts) != 1 or len(message_finishes) != 1:
            raise OpenCodeProtocolError(
                "expected exactly one start and finish for each OpenCode step"
            )
        finish_index, _finish = message_finishes[0]
        if finish_index <= message_starts[0]:
            raise OpenCodeProtocolError("OpenCode step finish preceded its start")


def _opencode_tool_time(value: Any) -> None:
    if not isinstance(value, dict):
        raise OpenCodeProtocolError("Flow tool execution time must be an object")
    start = value.get("start")
    end = value.get("end")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or start <= 0
        or end < start
    ):
        raise OpenCodeProtocolError("Flow tool execution time is invalid")


def _opencode_structured_result(output: str) -> dict[str, Any]:
    try:
        decoded = json.loads(output, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise OpenCodeProtocolError("Flow tool output is not structured JSON") from error
    if not isinstance(decoded, dict):
        raise OpenCodeProtocolError("Flow tool structured result must be an object")
    return cast(dict[str, Any], decoded)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _codex_structured_result(result: dict[str, Any]) -> dict[str, Any]:
    structured = result.get("structured_content")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if not isinstance(content, list):
        raise CodexProtocolError("Flow MCP result is missing structured content")
    texts = [
        item.get("text")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    if len(texts) != 1 or not isinstance(texts[0], str):
        raise CodexProtocolError("Flow MCP result text is ambiguous")
    try:
        decoded = json.loads(texts[0])
    except json.JSONDecodeError as error:
        raise CodexProtocolError("Flow MCP result text is not structured JSON") from error
    if not isinstance(decoded, dict):
        raise CodexProtocolError("Flow MCP structured result must be an object")
    return cast(dict[str, Any], decoded)


def _codex_required_string(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise CodexProtocolError(f"{label} is missing {key}")
    return value


def _codex_flow_configuration(path: Path) -> dict[str, Any]:
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        flow = parsed["mcp_servers"]["flow"]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as error:
        raise ValueError("mcp_config must contain the committed Flow server table") from error
    expected = {
        "enabled": True,
        "required": True,
        "command": "flow-mcp",
        "args": [],
        "env": {"FLOW_LOG_LEVEL": "INFO"},
        "startup_timeout_sec": 30.0,
        "tool_timeout_sec": 120.0,
    }
    if flow != expected:
        raise ValueError("mcp_config Flow server table does not match the verified contract")
    return cast(dict[str, Any], flow)


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
