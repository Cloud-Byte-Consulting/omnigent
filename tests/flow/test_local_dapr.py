from pathlib import Path

import pytest

from omnigent.flow.local_dapr import (
    APP_ID,
    CLI_VERSION,
    GRPC_PORT,
    HTTP_PORT,
    RUNTIME_VERSION,
    clean_reset_commands,
    init_command,
    safe_workflow_history,
    safe_workflow_list,
    start_command,
)


def test_initialize_command_pins_runtime_and_persistent_scheduler() -> None:
    assert init_command() == (
        "dapr",
        "init",
        "--runtime-version",
        RUNTIME_VERSION,
        "--scheduler-volume",
        "dapr_scheduler",
    )
    assert CLI_VERSION == "1.18.0"


def test_start_command_has_stable_identity_ports_and_resources() -> None:
    command = start_command(Path("/repo"), python="python3")

    assert command == (
        "dapr",
        "run",
        "--app-id",
        APP_ID,
        "--dapr-http-port",
        str(HTTP_PORT),
        "--dapr-grpc-port",
        str(GRPC_PORT),
        "--resources-path",
        "/repo/deploy/flow/dapr/components",
        "--",
        "python3",
        "-m",
        "omnigent.flow.smoke_worker",
    )


def test_clean_reset_requires_explicit_confirmation() -> None:
    with pytest.raises(ValueError, match="--yes"):
        clean_reset_commands(confirmed=False)

    assert clean_reset_commands(confirmed=True) == (
        ("dapr", "stop", "--app-id", APP_ID),
        ("dapr", "uninstall", "--all"),
        init_command(),
    )


def test_safe_inspection_projects_runtime_fields_without_payloads() -> None:
    listed = safe_workflow_list(
        [
            {
                "appID": APP_ID,
                "name": "FlowDagWorkflow",
                "instanceID": "run-failed",
                "runtimeStatus": "COMPLETED",
                "created": "2026-07-20T01:00:00Z",
                "lastUpdate": "2026-07-20T01:00:02Z",
                "customStatus": (
                    '{"status":"failed","nodes":{"A":{"status":"failed",'
                    '"attempt":2,"failure":{"category":"invalid_output",'
                    '"retryable":false,"message":"safe failure",'
                    '"rawPayload":"secret"}}}}'
                ),
                "failureMessage": "provider payload must not escape",
            }
        ]
    )
    history = safe_workflow_history(
        [
            {
                "type": "TaskCompleted",
                "name": "ExecuteFlowNode",
                "eventId": 4,
                "timestamp": "2026-07-20T01:00:02Z",
                "elapsed": "2s",
                "status": "COMPLETED",
                "executionId": "execution-1",
                "attrs": {"output": "secret"},
                "details": "secret",
            }
        ]
    )

    assert listed == [
        {
            "appID": APP_ID,
            "name": "FlowDagWorkflow",
            "instanceID": "run-failed",
            "runtimeStatus": "COMPLETED",
            "created": "2026-07-20T01:00:00Z",
            "lastUpdate": "2026-07-20T01:00:02Z",
            "flowStatus": {
                "status": "failed",
                "nodes": {
                    "A": {
                        "status": "failed",
                        "attempt": 2,
                        "failure": {
                            "category": "invalid_output",
                            "retryable": False,
                        },
                    }
                },
            },
        }
    ]
    assert history == [
        {
            "type": "TaskCompleted",
            "name": "ExecuteFlowNode",
            "eventId": 4,
            "timestamp": "2026-07-20T01:00:02Z",
            "elapsed": "2s",
            "status": "COMPLETED",
            "executionId": "execution-1",
        }
    ]
    serialized = str((listed, history))
    assert "secret" not in serialized
    assert "rawPayload" not in serialized
