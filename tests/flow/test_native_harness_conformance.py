import json
from typing import Any

import pytest

from tests.flow.native_harness import (
    FLOW_TOOLS,
    CopilotProtocolError,
    build_copilot_command,
    parse_copilot_tool_execution,
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
