import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

os.environ["DAPR_GRPC_PORT"] = "50101"
os.environ["DAPR_HTTP_PORT"] = "3510"

from omnigent.flow.local_dapr import APP_ID, readiness, start_command
from omnigent.flow.orchestration import FLOW_WORKFLOW_NAME, derive_node_execution_id

pytestmark = pytest.mark.skipif(
    os.environ.get("FLOW_DAPR_E2E") != "1",
    reason="set FLOW_DAPR_E2E=1 to exercise destructive local Dapr recovery",
)


def _wait_until(predicate, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError("Dapr condition did not become ready")


def _start(repo: Path) -> subprocess.Popen[str]:
    env = {
        **os.environ,
        "FLOW_FAKE_SLOW_NODE": "B",
        "FLOW_FAKE_INVALID_NODE": "FAIL",
        "FLOW_FAKE_DELAY_SECONDS": "20",
    }
    return subprocess.Popen(
        start_command(repo, python=sys.executable),
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _graceful_stop(process: subprocess.Popen[str]) -> None:
    app_pid, sidecar_pid = _registered_pids()
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    subprocess.run(
        ("dapr", "stop", "--app-id", APP_ID),
        check=False,
        capture_output=True,
        text=True,
    )
    _kill_pid(app_pid)
    _kill_pid(sidecar_pid)


def _crash(process: subprocess.Popen[str]) -> None:
    app_pid, sidecar_pid = _registered_pids()
    _kill_pid(app_pid)
    if process.poll() is None:
        process.kill()
    if process.poll() is None:
        process.wait(timeout=10)
    subprocess.run(
        ("dapr", "stop", "--app-id", APP_ID),
        check=False,
        capture_output=True,
        text=True,
    )
    _kill_pid(sidecar_pid)
    _wait_until(lambda: readiness()["sidecar"] is False, timeout=20)


def _registered_pids() -> tuple[int | None, int | None]:
    result = subprocess.run(
        ("dapr", "list", "--output", "json"),
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        apps = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, None
    app = next(
        (item for item in apps if isinstance(item, dict) and item.get("appId") == APP_ID),
        {},
    )
    app_pid = app.get("appPid")
    sidecar_pid = app.get("daprdPid")
    return (
        app_pid if isinstance(app_pid, int) and app_pid > 0 else None,
        sidecar_pid if isinstance(sidecar_pid, int) and sidecar_pid > 0 else None,
    )


def _kill_pid(pid: int | None) -> None:
    if pid is None:
        return
    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def _effect(client, run_id: str, node_id: str) -> dict | None:
    identity = derive_node_execution_id(run_id, node_id)
    response = client.get_state("flowstatestore", f"flow-fake-effect:{identity}")
    return json.loads(response.data) if response.data else None


def _fixture(run_id: str) -> dict:
    value_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    return {
        "runId": run_id,
        "approvedDagDigest": "sha256:approved-three-node-fixture",
        "dagSpec": {
            "version": "1.0",
            "nodes": [
                {
                    "id": "A",
                    "instructions": "Produce A",
                    "model": "fake:deterministic",
                    "outputSchema": value_schema,
                },
                {
                    "id": "B",
                    "instructions": "Produce B",
                    "model": "fake:deterministic",
                    "outputSchema": value_schema,
                },
                {
                    "id": "C",
                    "instructions": "Join A and B",
                    "dependsOn": ["A", "B"],
                    "model": "fake:deterministic",
                    "outputSchema": {
                        "type": "object",
                        "properties": {
                            "values": {
                                "type": "array",
                                "items": {"type": "string"},
                            }
                        },
                        "required": ["values"],
                        "additionalProperties": False,
                    },
                },
            ],
            "caps": {
                "maxNodes": 3,
                "maxRounds": 1,
                "maxConcurrent": 2,
                "tokenBudget": 3,
            },
        },
        "persistedResults": {},
    }


def _failed_fixture(run_id: str) -> dict:
    return {
        "runId": run_id,
        "approvedDagDigest": "sha256:approved-failed-inspection-fixture",
        "dagSpec": {
            "version": "1.0",
            "nodes": [
                {
                    "id": "FAIL",
                    "instructions": "Return an invalid result twice",
                    "model": "fake:deterministic",
                    "outputSchema": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                }
            ],
            "caps": {
                "maxNodes": 1,
                "maxRounds": 1,
                "maxConcurrent": 1,
                "tokenBudget": 2,
            },
        },
        "persistedResults": {},
    }


def test_three_node_dag_recovers_mid_wave_without_duplicate_effects() -> None:
    from dapr.clients import DaprClient
    from dapr.ext.workflow import DaprWorkflowClient

    from omnigent.flow.audit import DaprAuditStore, create_audit_event
    from omnigent.flow.caps import DaprCapStore
    from omnigent.flow.status import WorkflowStatusService
    from omnigent.flow.usage import ConservativeUsagePolicy, DaprUsageStore, UsageService

    repo = Path(__file__).parents[2]
    app_pid, sidecar_pid = _registered_pids()
    _kill_pid(app_pid)
    _kill_pid(sidecar_pid)
    subprocess.run(
        ("dapr", "stop", "--app-id", APP_ID),
        check=False,
        capture_output=True,
        text=True,
    )
    _wait_until(lambda: readiness()["sidecar"] is False, timeout=20)
    process = _start(repo)
    try:
        _wait_until(lambda: all(readiness().values()))
        state_client = DaprClient()
        workflow_client = DaprWorkflowClient()
        run_id = f"flow-three-node-{uuid4()}"
        fixture = _fixture(run_id)
        audit = DaprAuditStore(state_client)
        audit.append(
            create_audit_event(
                run_id=run_id,
                node_id=None,
                event_type="approval",
                timestamp=datetime.now(UTC),
                source="operator",
                correlation_key=f"{run_id}:approval",
                summary="Approved deterministic E2E fixture",
                metadata={"decision": "approved", "approver": "e2e-operator"},
            )
        )
        workflow_client.schedule_new_workflow(
            FLOW_WORKFLOW_NAME,
            input=fixture,
            instance_id=run_id,
        )
        assert workflow_client.wait_for_workflow_start(run_id, timeout_in_seconds=20)

        _wait_until(
            lambda: (
                (_effect(state_client, run_id, "A") or {}).get("completed") is True
                and (_effect(state_client, run_id, "B") or {}).get("deliveryCount") == 1
                and (_effect(state_client, run_id, "B") or {}).get("completed") is False
            )
        )
        _crash(process)

        process = _start(repo)
        _wait_until(lambda: all(readiness().values()))
        assert process.poll() is None
        state_client = DaprClient()
        workflow_client = DaprWorkflowClient()
        completed = workflow_client.wait_for_workflow_completion(
            run_id,
            timeout_in_seconds=45,
        )
        assert completed is not None
        assert completed.runtime_status.name == "COMPLETED"
        output = json.loads(completed.serialized_output)

        assert output["status"] == "succeeded"
        assert output["nodes"]["A"]["output"] == {"value": "A"}
        assert output["nodes"]["B"]["output"] == {"value": "B"}
        assert output["nodes"]["C"]["output"] == {"values": ["A", "B"]}
        assert [event["sequence"] for event in output["events"]] == list(
            range(1, len(output["events"]) + 1)
        )
        event_pairs = [(event["type"], event.get("nodeId")) for event in output["events"]]
        assert event_pairs.index(("node_scheduled", "A")) < event_pairs.index(
            ("node_succeeded", "A")
        )
        assert event_pairs.index(("node_scheduled", "B")) < event_pairs.index(
            ("node_succeeded", "A")
        )
        assert event_pairs.index(("node_succeeded", "B")) < event_pairs.index(
            ("node_scheduled", "C")
        )

        effects = {node: _effect(state_client, run_id, node) for node in ("A", "B", "C")}
        assert effects["A"]["deliveryCount"] == 1
        assert effects["B"]["deliveryCount"] >= 2
        assert effects["C"]["deliveryCount"] == 1
        assert all(record["effectCount"] == 1 for record in effects.values())
        assert all(record["completed"] is True for record in effects.values())

        status = WorkflowStatusService(
            workflow_client,
            audit=DaprAuditStore(state_client),
            usage=UsageService(
                DaprUsageStore(state_client),
                missing_usage_policy=ConservativeUsagePolicy(1),
            ),
            caps=DaprCapStore(state_client),
            authorizer=lambda actor, candidate: actor == "operator" and candidate == run_id,
        ).get(run_id, actor="operator")
        assert status["state"] == "succeeded"
        assert status["approval"]["approved"] is True
        assert status["caps"]["utilization"]["usedTokens"] == 3
        assert all(status["nodes"][node]["state"] == "succeeded" for node in effects)
        assert [event["type"] for event in status["history"]] == ["approval"]

        history = subprocess.run(
            ("dapr", "workflow", "history", run_id, "--app-id", APP_ID),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert FLOW_WORKFLOW_NAME in history
        assert "ExecuteFlowNode" in history
        assert "COMPLETED" in history
    finally:
        _graceful_stop(process)


def test_failed_activity_is_visible_in_safe_dapr_and_flow_views() -> None:
    from dapr.clients import DaprClient
    from dapr.ext.workflow import DaprWorkflowClient

    from omnigent.flow.audit import DaprAuditStore
    from omnigent.flow.caps import DaprCapStore
    from omnigent.flow.status import WorkflowStatusService
    from omnigent.flow.usage import ConservativeUsagePolicy, DaprUsageStore, UsageService

    repo = Path(__file__).parents[2]
    process = _start(repo)
    try:
        _wait_until(lambda: all(readiness().values()))
        state_client = DaprClient()
        workflow_client = DaprWorkflowClient()
        run_id = f"flow-failed-inspection-{uuid4()}"
        workflow_client.schedule_new_workflow(
            FLOW_WORKFLOW_NAME,
            input=_failed_fixture(run_id),
            instance_id=run_id,
        )
        completed = workflow_client.wait_for_workflow_completion(
            run_id,
            timeout_in_seconds=30,
        )
        assert completed is not None
        output = json.loads(completed.serialized_output)
        assert output["status"] == "failed"
        assert output["nodes"]["FAIL"]["attempt"] == 2

        status = WorkflowStatusService(
            workflow_client,
            audit=DaprAuditStore(state_client),
            usage=UsageService(
                DaprUsageStore(state_client),
                missing_usage_policy=ConservativeUsagePolicy(1),
            ),
            caps=DaprCapStore(state_client),
            authorizer=lambda actor, candidate: actor == "operator" and candidate == run_id,
        ).get(run_id, actor="operator")
        assert status["state"] == "failed"
        assert status["nodes"]["FAIL"]["attempts"] == 2
        assert status["nodes"]["FAIL"]["failure"] == {
            "category": "invalid_output",
            "retryable": False,
            "message": "provider output does not match outputSchema",
            "policyExhausted": True,
        }

        listed = subprocess.run(
            (sys.executable, "-m", "omnigent.flow.local_dapr", "inspect-list"),
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        safe_rows = json.loads(listed.stdout)
        safe_row = next(row for row in safe_rows if row.get("instanceID") == run_id)
        assert safe_row["flowStatus"]["status"] == "failed"
        assert safe_row["flowStatus"]["nodes"]["FAIL"] == {
            "attempt": 2,
            "failure": {"category": "invalid_output", "retryable": False},
            "status": "failed",
        }

        history = subprocess.run(
            (
                sys.executable,
                "-m",
                "omnigent.flow.local_dapr",
                "inspect-history",
                run_id,
            ),
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        safe_history = json.loads(history.stdout)
        assert any(event.get("name") == "ExecuteFlowNode" for event in safe_history)
        assert any("timestamp" in event for event in safe_history)
        assert "attrs" not in history.stdout
        assert "details" not in history.stdout
    finally:
        _graceful_stop(process)
