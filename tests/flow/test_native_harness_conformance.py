import json
from typing import Any

import pytest

from tests.flow.native_harness import (
    FLOW_TOOLS,
    CodexProtocolError,
    CopilotProtocolError,
    CursorProtocolError,
    build_codex_command,
    build_copilot_command,
    build_cursor_command,
    parse_codex_tool_execution,
    parse_copilot_tool_execution,
    parse_cursor_tool_execution,
    redact_sensitive,
)


def _event(event_type: str, data: dict[str, Any]) -> str:
    return json.dumps({"type": event_type, "data": data})


def _successful_output(
    *,
    tool: str = "run_workflow",
    result: dict[str, Any] | None = None,
    arguments: dict[str, Any] | None = None,
) -> str:
    return "\n".join(
        (
            _event("session.start", {"version": 1}),
            _event(
                "tool.execution_start",
                {
                    "toolCallId": "call-1",
                    "toolName": f"flow-{tool}",
                    "mcpServerName": "flow",
                    "mcpToolName": tool,
                    "arguments": arguments or {"confirm": False},
                },
            ),
            _event(
                "tool.execution_complete",
                {
                    "toolCallId": "call-1",
                    "success": True,
                    "result": {
                        "content": "summary",
                        "structuredContent": result or {"status": "approval_required"},
                    },
                },
            ),
        )
    )


def test_command_is_noninteractive_and_exposes_only_four_flow_tools() -> None:
    command = build_copilot_command("Call the requested Flow tool")

    assert command[:3] == ("copilot", "--prompt", "Call the requested Flow tool")
    assert (
        command[command.index("--additional-mcp-config")],
        command[command.index("--additional-mcp-config") + 1],
    ) == ("--additional-mcp-config", "@.mcp.json")
    available = tuple(
        command[index + 1]
        for index, argument in enumerate(command)
        if argument == "--available-tools"
    )
    allowed = tuple(
        command[index + 1] for index, argument in enumerate(command) if argument == "--allow-tool"
    )
    assert available == tuple(f"flow-{tool}" for tool in FLOW_TOOLS)
    assert allowed == tuple(f"flow({tool})" for tool in FLOW_TOOLS)
    assert "--disable-builtin-mcps" in command
    assert "--allow-all" not in command
    assert "--allow-all-tools" not in command
    assert "shell" not in " ".join(command)


def test_command_accepts_explicit_binary_and_config_without_shell_interpolation() -> None:
    command = build_copilot_command(
        "Use Flow",
        executable="/tmp/copilot cli",
        mcp_config="configs/flow.json",
    )

    assert command[0] == "/tmp/copilot cli"
    assert "@configs/flow.json" in command
    with pytest.raises(ValueError, match="prompt"):
        build_copilot_command("   ")
    with pytest.raises(ValueError, match="without an @"):
        build_copilot_command("Use Flow", mcp_config="@.mcp.json")


def test_parser_correlates_connected_flow_server_and_structured_result() -> None:
    output = _successful_output(
        result={"status": "queued", "runId": "run-1"},
        arguments={"confirm": True},
    )

    execution = parse_copilot_tool_execution(output, expected_tool="run_workflow")

    assert execution.server_name == "flow"
    assert execution.tool_name == "run_workflow"
    assert execution.model_tool_name == "flow-run_workflow"
    assert execution.arguments == {"confirm": True}
    assert execution.structured_result == {"status": "queued", "runId": "run-1"}


def test_parser_supports_json_content_fallback() -> None:
    output = "\n".join(
        (
            _event(
                "tool.execution_start",
                {
                    "toolCallId": "call-1",
                    "toolName": "flow-list_workflows",
                    "mcpServerName": "flow",
                    "mcpToolName": "list_workflows",
                    "arguments": {},
                },
            ),
            _event(
                "tool.execution_complete",
                {
                    "toolCallId": "call-1",
                    "success": True,
                    "result": {"content": json.dumps({"visibleCount": 0})},
                },
            ),
        )
    )

    execution = parse_copilot_tool_execution(output, expected_tool="list_workflows")

    assert execution.structured_result == {"visibleCount": 0}


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("not-json", "not valid JSON"),
        (_event("session.start", {}), "exactly one tool execution start"),
        (
            _successful_output().replace('"mcpServerName": "flow"', '"mcpServerName": "other"'),
            "expected Flow tool",
        ),
        (
            _successful_output().replace(
                '"mcpToolName": "run_workflow"', '"mcpToolName": "other"'
            ),
            "expected Flow tool",
        ),
        (
            _successful_output().replace('"toolName": "flow-run_workflow"', '"toolName": "other"'),
            "noncanonical",
        ),
        (
            _successful_output().replace('"success": true', '"success": false'),
            "did not succeed",
        ),
        (
            "\n".join(_successful_output().splitlines()[:2]),
            "matching tool completion",
        ),
    ],
)
def test_parser_fails_closed_on_malformed_or_missing_protocol_events(
    output: str,
    message: str,
) -> None:
    with pytest.raises(CopilotProtocolError, match=message):
        parse_copilot_tool_execution(output, expected_tool="run_workflow")


def test_parser_rejects_duplicate_or_unrelated_tool_executions() -> None:
    output = _successful_output()
    duplicate_start = _event(
        "tool.execution_start",
        {
            "toolCallId": "call-2",
            "toolName": "flow-list_workflows",
            "mcpServerName": "flow",
            "mcpToolName": "list_workflows",
            "arguments": {},
        },
    )

    with pytest.raises(CopilotProtocolError, match="exactly one tool execution start"):
        parse_copilot_tool_execution(f"{output}\n{duplicate_start}", expected_tool="run_workflow")
    with pytest.raises(ValueError, match="unknown canonical"):
        parse_copilot_tool_execution(output, expected_tool="delete_everything")


def test_safe_evidence_redacts_tokens_and_secrets_without_hiding_usage() -> None:
    private_key = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
    output = _successful_output(
        arguments={
            "approval_token": "opaque-approval",
            "tokenBudget": 3,
            "authorization": "Bearer raw-auth-token",
        },
        result={
            "approvalToken": "opaque-result-token",
            "credentials": {"api": "sk-live-sensitive"},
            "history": [{"summary": private_key}],
            "usage": {"totalTokens": 3},
        },
    )

    evidence = parse_copilot_tool_execution(output, expected_tool="run_workflow").safe_evidence()
    encoded = json.dumps(evidence)

    assert "opaque-approval" not in encoded
    assert "opaque-result-token" not in encoded
    assert "sk-live-sensitive" not in encoded
    assert "abc" not in encoded
    assert evidence["arguments"]["approval_token"] == "[REDACTED]"
    assert evidence["arguments"]["tokenBudget"] == 3
    assert evidence["result"]["usage"]["totalTokens"] == 3
    assert "arguments=" not in repr(
        parse_copilot_tool_execution(output, expected_tool="run_workflow")
    )


def test_redaction_covers_nested_collections_and_inline_credentials() -> None:
    value = {
        "items": [
            {"clientSigningKey": "key-value", "api_key": "api-value"},
            "Bearer token-value",
            "provider sk-test-secret-value",
        ]
    }

    redacted = redact_sensitive(value)

    assert redacted == {
        "items": [
            {"clientSigningKey": "[REDACTED]", "api_key": "[REDACTED]"},
            "Bearer [REDACTED]",
            "provider [REDACTED]",
        ]
    }


def _successful_codex_output(
    *,
    tool: str = "run_workflow",
    result: dict[str, Any] | None = None,
    arguments: dict[str, Any] | None = None,
) -> str:
    item = {
        "id": "item-1",
        "type": "mcp_tool_call",
        "server": "flow",
        "tool": tool,
        "arguments": arguments or {"confirm": False},
        "result": None,
        "error": None,
        "status": "in_progress",
    }
    completed = {
        **item,
        "result": {
            "structured_content": result or {"status": "approval_required"},
        },
        "status": "completed",
    }
    return "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.started", "item": item}),
            json.dumps({"type": "item.completed", "item": completed}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        )
    )


def test_codex_command_is_ephemeral_noninteractive_and_tool_scoped() -> None:
    command = build_codex_command(
        "run_workflow",
        executable="/tmp/codex cli",
        flow_entrypoint="/tmp/installed/bin/flow-mcp",
    )

    assert command[:2] == ("/tmp/codex cli", "exec")
    assert command[-1] == "-"
    for flag in (
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--sandbox",
        "--json",
    ):
        assert flag in command
    encoded = " ".join(command)
    assert 'mcp_servers.flow.enabled_tools=["run_workflow"]' in encoded
    assert 'mcp_servers.flow.command="/tmp/installed/bin/flow-mcp"' in encoded
    assert "FLOW_SIGNING_KEY" in encoded
    assert "dangerously-bypass" not in encoded
    assert "approval-token" not in encoded
    with pytest.raises(ValueError, match="unknown canonical"):
        build_codex_command("delete_everything")


def test_codex_parser_correlates_one_completed_flow_call() -> None:
    output = _successful_codex_output(
        arguments={"confirm": True},
        result={"status": "queued", "runId": "run-1"},
    )

    execution = parse_codex_tool_execution(output, expected_tool="run_workflow")

    assert execution.server_name == "flow"
    assert execution.tool_name == "run_workflow"
    assert execution.arguments == {"confirm": True}
    assert execution.structured_result == {"status": "queued", "runId": "run-1"}


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("not-json", "not valid JSON"),
        (json.dumps({"type": "turn.completed"}), "thread and turn lifecycle"),
        (
            _successful_codex_output().replace('"server": "flow"', '"server": "other"'),
            "expected Flow tool",
        ),
        (
            _successful_codex_output().replace('"status": "completed"', '"status": "failed"'),
            "did not complete",
        ),
        (
            _successful_codex_output().replace(
                '"type": "turn.completed"', '"type": "turn.failed"'
            ),
            "turn failed",
        ),
    ],
)
def test_codex_parser_fails_closed_on_invalid_protocol(
    output: str,
    message: str,
) -> None:
    with pytest.raises(CodexProtocolError, match=message):
        parse_codex_tool_execution(output, expected_tool="run_workflow")


def test_codex_parser_rejects_unrelated_or_duplicate_tool_actions() -> None:
    output = _successful_codex_output()
    unrelated = json.dumps(
        {
            "type": "item.completed",
            "item": {"id": "cmd", "type": "command_execution", "status": "completed"},
        }
    )

    with pytest.raises(CodexProtocolError, match="unrelated action"):
        parse_codex_tool_execution(f"{output}\n{unrelated}", expected_tool="run_workflow")
    with pytest.raises(CodexProtocolError, match="coherent Codex thread"):
        parse_codex_tool_execution(f"{output}\n{output}", expected_tool="run_workflow")


def test_codex_parser_rejects_malformed_start_and_updated_items() -> None:
    malformed_start = _successful_codex_output().replace(
        '"result": null, "error": null, "status": "in_progress"',
        '"result": {}, "error": "failed", "status": "failed"',
        1,
    )
    lines = _successful_codex_output().splitlines()
    lines.insert(3, json.dumps({"type": "item.updated", "item": None}))

    with pytest.raises(CodexProtocolError, match="start event is malformed"):
        parse_codex_tool_execution(malformed_start, expected_tool="run_workflow")
    with pytest.raises(CodexProtocolError, match="item object"):
        parse_codex_tool_execution("\n".join(lines), expected_tool="run_workflow")


def _cursor_event(event_type: str, **fields: Any) -> str:
    return json.dumps({"type": event_type, **fields})


def _cursor_mcp_call(
    *,
    tool: str = "run_workflow",
    arguments: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    call: dict[str, Any] = {
        "args": {
            "name": f"flow-{tool}",
            "toolCallId": "call-1",
            "providerIdentifier": "flow",
            "toolName": tool,
            "args": arguments or {"confirm": False},
        }
    }
    if result is not None:
        call["result"] = result
    return {"mcpToolCall": call}


def _successful_cursor_output(
    *,
    tool: str = "run_workflow",
    arguments: dict[str, Any] | None = None,
    structured_result: dict[str, Any] | None = None,
    protobuf_text: bool = False,
) -> str:
    result = structured_result or {"status": "approval_required"}
    text: Any = json.dumps(result)
    if protobuf_text:
        text = {"text": text}
    return "\n".join(
        (
            _cursor_event(
                "system",
                subtype="init",
                session_id="session-1",
                model="cursor-small",
            ),
            _cursor_event(
                "user",
                session_id="session-1",
                message={"role": "user", "content": [{"type": "text", "text": "call"}]},
            ),
            _cursor_event(
                "tool_call",
                subtype="started",
                call_id="call-1",
                session_id="session-1",
                tool_call=_cursor_mcp_call(tool=tool, arguments=arguments),
            ),
            _cursor_event(
                "tool_call",
                subtype="completed",
                call_id="call-1",
                session_id="session-1",
                tool_call=_cursor_mcp_call(
                    tool=tool,
                    arguments=arguments,
                    result={
                        "success": {
                            "content": [{"text": text}],
                            "isError": False,
                        }
                    },
                ),
            ),
            _cursor_event(
                "result",
                subtype="success",
                is_error=False,
                result="done",
                session_id="session-1",
            ),
        )
    )


def test_cursor_command_is_sandboxed_headless_and_tool_scoped() -> None:
    command = build_cursor_command(
        expected_tool="run_workflow",
        executable="/tmp/cursor agent",
        workspace="/tmp/flow workspace",
    )

    assert command == (
        "/tmp/cursor agent",
        "--print",
        "--output-format",
        "stream-json",
        "--trust",
        "--approve-mcps",
        "--sandbox",
        "enabled",
        "--workspace",
        "/tmp/flow workspace",
    )
    assert "--force" not in command
    assert "--yolo" not in command
    assert "approval_token" not in " ".join(command)


def test_cursor_command_rejects_unscoped_or_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="unknown canonical"):
        build_cursor_command(expected_tool="delete_everything")
    with pytest.raises(ValueError, match="workspace"):
        build_cursor_command(expected_tool="run_workflow", workspace="")


@pytest.mark.parametrize("protobuf_text", [False, True])
def test_cursor_parser_correlates_one_successful_mcp_call(protobuf_text: bool) -> None:
    output = _successful_cursor_output(
        arguments={"confirm": True},
        structured_result={"status": "queued", "runId": "run-1"},
        protobuf_text=protobuf_text,
    )

    execution = parse_cursor_tool_execution(output, expected_tool="run_workflow")

    assert execution.session_id == "session-1"
    assert execution.call_id == "call-1"
    assert execution.server_name == "flow"
    assert execution.tool_name == "run_workflow"
    assert execution.arguments == {"confirm": True}
    assert execution.structured_result == {"status": "queued", "runId": "run-1"}


def test_cursor_parser_accepts_matching_structured_and_text_results() -> None:
    lines = _successful_cursor_output().splitlines()
    completed = json.loads(lines[3])
    success = completed["tool_call"]["mcpToolCall"]["result"]["success"]
    success["structuredContent"] = {"status": "approval_required"}
    lines[3] = json.dumps(completed)

    execution = parse_cursor_tool_execution("\n".join(lines), expected_tool="run_workflow")

    assert execution.structured_result == {"status": "approval_required"}


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("not-json", "not valid JSON"),
        (_cursor_event("system", subtype="init", session_id="session-1"), "end with"),
        (
            _successful_cursor_output().replace('"subtype": "init"', '"subtype": "retry"'),
            "start with system init",
        ),
        (
            _successful_cursor_output().replace(
                '"session_id": "session-1"', '"session_id": "session-2"', 1
            ),
            "one session_id",
        ),
        (
            _successful_cursor_output().replace(
                '"providerIdentifier": "flow"', '"providerIdentifier": "other"'
            ),
            "expected Flow tool",
        ),
        (
            _successful_cursor_output().replace(
                '"toolName": "run_workflow"', '"toolName": "list_workflows"'
            ),
            "expected Flow tool",
        ),
        (
            _successful_cursor_output().replace('"is_error": false', '"is_error": true'),
            "result did not succeed",
        ),
        (
            _successful_cursor_output().replace('"isError": false', '"isError": true'),
            "marked as an error",
        ),
        (
            _successful_cursor_output().replace('"success": {', '"error": {', 1),
            "not successful",
        ),
    ],
)
def test_cursor_parser_fails_closed_on_invalid_protocol(
    output: str,
    message: str,
) -> None:
    with pytest.raises(CursorProtocolError, match=message):
        parse_cursor_tool_execution(output, expected_tool="run_workflow")


def test_cursor_parser_rejects_duplicate_unrelated_and_retry_events() -> None:
    lines = _successful_cursor_output().splitlines()
    duplicate = lines[2].replace('"call-1"', '"call-2"')
    unrelated = _cursor_event(
        "tool_call",
        subtype="started",
        call_id="other",
        session_id="session-1",
        tool_call={"shellToolCall": {"args": {"command": "pwd"}}},
    )
    retry = _cursor_event("system", subtype="retry", session_id="session-1")

    with pytest.raises(CursorProtocolError, match="exactly one"):
        parse_cursor_tool_execution(
            "\n".join((*lines[:-1], duplicate, lines[-1])),
            expected_tool="run_workflow",
        )
    with pytest.raises(CursorProtocolError, match="exactly one"):
        parse_cursor_tool_execution(
            "\n".join((*lines[:-1], unrelated, lines[-1])),
            expected_tool="run_workflow",
        )
    with pytest.raises(CursorProtocolError, match="coherent Cursor lifecycle"):
        parse_cursor_tool_execution(
            "\n".join((*lines[:-1], retry, lines[-1])),
            expected_tool="run_workflow",
        )


def test_cursor_parser_rejects_mismatched_ids_and_typed_arguments() -> None:
    lines = _successful_cursor_output(arguments={"confirm": True}).splitlines()
    wrong_call = json.loads(lines[3])
    wrong_call["call_id"] = "call-2"
    typed_mismatch = json.loads(lines[3])
    typed_mismatch["tool_call"]["mcpToolCall"]["args"]["args"] = {"confirm": 1}
    nested_mismatch = json.loads(lines[3])
    nested_mismatch["tool_call"]["mcpToolCall"]["args"]["toolCallId"] = "call-2"

    for replacement, message in (
        (wrong_call, "does not match"),
        (typed_mismatch, "arguments do not match"),
        (nested_mismatch, "nested toolCallId"),
    ):
        changed = [*lines]
        changed[3] = json.dumps(replacement)
        with pytest.raises(CursorProtocolError, match=message):
            parse_cursor_tool_execution("\n".join(changed), expected_tool="run_workflow")


@pytest.mark.parametrize(
    "success",
    [
        {"content": []},
        {"content": [{"text": "{}"}, {"text": "{}"}]},
        {"content": [{"text": "not-json"}]},
        {"content": [{"text": "[]"}]},
        {"content": [{"image": {"data": "abc"}}]},
        {"structuredContent": [], "content": [{"text": "{}"}]},
        {
            "structuredContent": {"status": "queued"},
            "content": [{"text": '{"status":"failed"}'}],
        },
    ],
)
def test_cursor_parser_rejects_missing_or_ambiguous_results(success: dict[str, Any]) -> None:
    lines = _successful_cursor_output().splitlines()
    completed = json.loads(lines[3])
    completed["tool_call"]["mcpToolCall"]["result"] = {"success": success}
    lines[3] = json.dumps(completed)

    with pytest.raises(CursorProtocolError):
        parse_cursor_tool_execution("\n".join(lines), expected_tool="run_workflow")


def test_cursor_safe_evidence_redacts_nested_secrets() -> None:
    output = _successful_cursor_output(
        arguments={"approval_token": "opaque", "tokenBudget": 3},
        structured_result={
            "credentials": {"key": "sk-test-secret"},
            "usage": {"totalTokens": 3},
        },
    )

    execution = parse_cursor_tool_execution(output, expected_tool="run_workflow")
    evidence = execution.safe_evidence()
    encoded = json.dumps(evidence)

    assert "opaque" not in encoded
    assert "sk-test-secret" not in encoded
    assert evidence["arguments"]["approval_token"] == "[REDACTED]"
    assert evidence["arguments"]["tokenBudget"] == 3
    assert evidence["result"]["usage"]["totalTokens"] == 3
    assert "arguments=" not in repr(execution)
