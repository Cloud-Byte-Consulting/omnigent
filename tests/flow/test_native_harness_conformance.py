import json
from pathlib import Path
from typing import Any

import pytest

from tests.flow.native_harness import (
    FLOW_TOOLS,
    CodexProtocolError,
    CopilotProtocolError,
    OpenCodeProtocolError,
    build_codex_command,
    build_copilot_command,
    build_opencode_command,
    parse_codex_tool_execution,
    parse_copilot_tool_execution,
    parse_opencode_tool_execution,
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


def _opencode_event(event_type: str, part: dict[str, Any]) -> str:
    return json.dumps(
        {
            "type": event_type,
            "timestamp": 1_784_636_764_865,
            "sessionID": "ses-1",
            "part": {
                "id": f"part-{event_type}",
                "sessionID": "ses-1",
                "messageID": "msg-1",
                **part,
            },
        }
    )


def _successful_opencode_output(
    *,
    tool: str = "run_workflow",
    arguments: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> str:
    return "\n".join(
        (
            _opencode_event("step_start", {"type": "step-start"}),
            _opencode_event(
                "tool_use",
                {
                    "type": "tool",
                    "callID": "call-1",
                    "tool": f"flow_{tool}",
                    "state": {
                        "status": "completed",
                        "input": arguments
                        or {"confirm": False, "attempt": 1, "labels": ["native"]},
                        "output": json.dumps(
                            result or {"status": "approval_required"},
                            separators=(",", ":"),
                        ),
                        "title": "",
                        "metadata": {"truncated": False},
                        "time": {"start": 1_784_636_764_800, "end": 1_784_636_764_864},
                    },
                },
            ),
            _opencode_event(
                "step_finish",
                {"type": "step-finish", "reason": "tool-calls"},
            ),
            _opencode_event(
                "text",
                {
                    "type": "text",
                    "text": "Flow result returned.",
                    "time": {"start": 1_784_636_764_866, "end": 1_784_636_764_867},
                },
            ),
        )
    )


def test_opencode_command_keeps_sensitive_prompt_out_of_argv(tmp_path: Path) -> None:
    config = tmp_path / "opencode.json"
    config.write_text("{}", encoding="utf-8")
    prompt = tmp_path / "instruction.txt"
    secret = "approval_token=temporary-native-secret"
    prompt.write_text(secret, encoding="utf-8")
    prompt.chmod(0o600)

    command = build_opencode_command(
        expected_tool="run_workflow",
        executable="/tmp/opencode cli",
        config=config,
        prompt_file=prompt,
    )

    assert command[:3] == (
        "/tmp/opencode cli",
        "run",
        "Execute only flow_run_workflow using the attached instructions. "
        "Do not use any other tool.",
    )
    assert command[-2:] == ("--file", str(prompt.resolve()))
    assert command.index("--file") > command.index("--dir")
    assert command[command.index("--dir") + 1] == str(tmp_path.resolve())
    assert command[command.index("--format") + 1] == "json"
    assert command[command.index("--agent") + 1] == "build"
    assert command[command.index("--model") + 1] == "opencode/deepseek-v4-flash-free"
    assert secret not in " ".join(command)
    for forbidden in ("--continue", "--session", "--fork", "--share", "--attach"):
        assert forbidden not in command


def test_opencode_command_rejects_unsafe_inputs(tmp_path: Path) -> None:
    config = tmp_path / "opencode.json"
    config.write_text("{}", encoding="utf-8")
    prompt = tmp_path / "instruction.txt"
    prompt.write_text("Use Flow", encoding="utf-8")
    prompt.chmod(0o600)

    with pytest.raises(ValueError, match="unknown canonical"):
        build_opencode_command(
            expected_tool="delete_everything", config=config, prompt_file=prompt
        )
    prompt.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        build_opencode_command(expected_tool="list_workflows", config=config, prompt_file=prompt)
    prompt.chmod(0o600)
    with pytest.raises(ValueError, match=r"opencode\.json"):
        build_opencode_command(
            expected_tool="list_workflows",
            config=tmp_path / "missing.json",
            prompt_file=prompt,
        )
    with pytest.raises(ValueError, match="provider/model"):
        build_opencode_command(
            expected_tool="list_workflows",
            config=config,
            prompt_file=prompt,
            model="--unsafe",
        )


def test_opencode_parser_preserves_exact_typed_arguments_and_result() -> None:
    arguments = {"confirm": False, "attempt": 1, "ratio": 1.0, "labels": ["native"]}
    result = {"status": "queued", "runId": "run-1"}

    execution = parse_opencode_tool_execution(
        _successful_opencode_output(arguments=arguments, result=result),
        expected_tool="run_workflow",
    )

    assert execution.call_id == "call-1"
    assert execution.session_id == "ses-1"
    assert execution.server_name == "flow"
    assert execution.tool_name == "run_workflow"
    assert execution.model_tool_name == "flow_run_workflow"
    assert execution.arguments == arguments
    assert execution.arguments["confirm"] is False
    assert type(execution.arguments["attempt"]) is int
    assert type(execution.arguments["ratio"]) is float
    assert execution.structured_result == result


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("not-json", "not valid JSON"),
        (
            _successful_opencode_output().replace(
                '"timestamp": 1784636764865', '"timestamp": true'
            ),
            "timestamp",
        ),
        (
            _successful_opencode_output().replace(
                '"sessionID": "ses-1"', '"sessionID": "ses-2"', 1
            ),
            "sessions do not match",
        ),
        (
            _successful_opencode_output().replace('"tool": "flow_run_workflow"', '"tool": "grep"'),
            "expected Flow tool",
        ),
        (
            _successful_opencode_output().replace('"status": "completed"', '"status": "error"'),
            "did not complete",
        ),
        (
            _successful_opencode_output().replace('"truncated": false', '"truncated": true'),
            "truncated",
        ),
        (
            _successful_opencode_output().replace(
                '"input": {"confirm": false, "attempt": 1, "labels": ["native"]}',
                '"input": []',
            ),
            "arguments must be an object",
        ),
        (
            _successful_opencode_output().replace('"reason": "tool-calls"', '"reason": "stop"'),
            "finish with a tool call",
        ),
    ],
)
def test_opencode_parser_fails_closed_on_invalid_protocol(
    output: str,
    message: str,
) -> None:
    with pytest.raises(OpenCodeProtocolError, match=message):
        parse_opencode_tool_execution(output, expected_tool="run_workflow")


def test_opencode_parser_rejects_unrelated_duplicate_error_and_retry_events() -> None:
    output = _successful_opencode_output()
    tool_line = output.splitlines()[1]
    error = json.dumps(
        {
            "type": "error",
            "timestamp": 1_784_636_764_900,
            "sessionID": "ses-1",
            "error": {"name": "ProviderError"},
        }
    )

    with pytest.raises(OpenCodeProtocolError, match="exactly one"):
        parse_opencode_tool_execution(f"{output}\n{tool_line}", expected_tool="run_workflow")
    unrelated = tool_line.replace('"flow_run_workflow"', '"read"')
    with pytest.raises(OpenCodeProtocolError, match="exactly one"):
        parse_opencode_tool_execution(f"{output}\n{unrelated}", expected_tool="run_workflow")
    with pytest.raises(OpenCodeProtocolError, match="error or unsupported"):
        parse_opencode_tool_execution(f"{output}\n{error}", expected_tool="run_workflow")
    with pytest.raises(ValueError, match="unknown canonical"):
        parse_opencode_tool_execution(output, expected_tool="delete_everything")


def test_opencode_parser_rejects_appended_or_duplicate_terminal_finishes() -> None:
    output = _successful_opencode_output()
    appended_error = _opencode_event(
        "step_finish",
        {"type": "step-finish", "reason": "error"},
    )
    appended_stop = _opencode_event(
        "step_finish",
        {"type": "step-finish", "reason": "stop"},
    )

    with pytest.raises(OpenCodeProtocolError, match="non-success terminal"):
        parse_opencode_tool_execution(f"{output}\n{appended_error}", expected_tool="run_workflow")
    with pytest.raises(OpenCodeProtocolError, match="exactly one start and finish"):
        parse_opencode_tool_execution(f"{output}\n{appended_stop}", expected_tool="run_workflow")


def test_opencode_parser_rejects_any_non_success_terminal_reason() -> None:
    output = _successful_opencode_output().replace('"reason": "tool-calls"', '"reason": "error"')

    with pytest.raises(OpenCodeProtocolError, match="non-success terminal"):
        parse_opencode_tool_execution(output, expected_tool="run_workflow")


def test_opencode_parser_requires_untruncated_structured_object_output() -> None:
    array_output = _successful_opencode_output().replace(
        '{\\"status\\":\\"approval_required\\"}', "[1,2]"
    )
    invalid_output = _successful_opencode_output().replace(
        '{\\"status\\":\\"approval_required\\"}', "not-json"
    )

    with pytest.raises(OpenCodeProtocolError, match="must be an object"):
        parse_opencode_tool_execution(array_output, expected_tool="run_workflow")
    with pytest.raises(OpenCodeProtocolError, match="not structured JSON"):
        parse_opencode_tool_execution(invalid_output, expected_tool="run_workflow")


def test_opencode_safe_evidence_redacts_secrets() -> None:
    output = _successful_opencode_output(
        arguments={"approval_token": "opaque-approval", "confirm": True},
        result={
            "approvalToken": "opaque-result",
            "credentials": {"provider": "sk-live-secret"},
            "usage": {"totalTokens": 3},
        },
    )

    execution = parse_opencode_tool_execution(output, expected_tool="run_workflow")
    evidence = execution.safe_evidence()
    encoded = json.dumps(evidence)

    assert "opaque-approval" not in encoded
    assert "opaque-result" not in encoded
    assert "sk-live-secret" not in encoded
    assert evidence["arguments"]["approval_token"] == "[REDACTED]"
    assert evidence["result"]["usage"]["totalTokens"] == 3
    assert "arguments=" not in repr(execution)
