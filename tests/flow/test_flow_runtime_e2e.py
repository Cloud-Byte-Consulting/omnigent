import asyncio
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
from omnigent.flow.orchestration import (
    FLOW_WORKFLOW_NAME,
    FlowWorkflowInput,
    derive_node_execution_id,
)

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


def _start(
    repo: Path,
    *,
    expansion_node: str | None = None,
    slow_node: str | None = "B",
    delay_seconds: float = 20,
) -> subprocess.Popen[str]:
    env = {
        **os.environ,
        "FLOW_MODE": "conformance",
        "FLOW_FAKE_INVALID_NODE": "FAIL",
        "FLOW_FAKE_DELAY_SECONDS": str(delay_seconds),
    }
    if slow_node is not None:
        env["FLOW_FAKE_SLOW_NODE"] = slow_node
    if expansion_node is not None:
        env["FLOW_FAKE_EXPANSION_NODE"] = expansion_node
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


def _denied_fixture(run_id: str) -> dict:
    value = _failed_fixture(run_id)
    value["approvedDagDigest"] = "sha256:approved-cap-denial-fixture"
    value["dagSpec"]["nodes"][0] = {
        "id": "DENIED",
        "instructions": "This provider call must never occur",
        "model": "fake:deterministic",
    }
    value["dagSpec"]["caps"]["tokenBudget"] = 1
    return value


def _expansion_fixture(run_id: str) -> dict:
    return {
        "runId": run_id,
        "approvedDagDigest": "sha256:approved-expansion-fixture",
        "dagSpec": {
            "version": "1.0",
            "nodes": [
                {
                    "id": "EXPAND",
                    "instructions": "Expand exactly once",
                    "model": "fake:deterministic",
                    "canExpand": True,
                }
            ],
            "caps": {
                "maxNodes": 2,
                "maxRounds": 2,
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
        pre_crash_history = audit.history(run_id)
        pre_crash_event_ids = [event.event_id for event in pre_crash_history]
        assert [event.type for event in pre_crash_history] == [
            "approval",
            "validation",
            "run_queued",
            "run_running",
            "node_queued",
            "node_queued",
            "node_queued",
            "dispatch",
            "node_running",
            "dispatch",
            "node_running",
        ]
        pre_crash_caps = DaprCapStore(state_client).state(
            run_id,
            FlowWorkflowInput.model_validate(fixture).dag_spec.caps,
        )
        assert pre_crash_caps.accepted_node_ids == ("A", "B", "C")
        assert pre_crash_caps.current_round == 1
        assert pre_crash_caps.running_node_ids == ("A", "B")
        assert pre_crash_caps.queued_node_ids == ("C",)
        assert pre_crash_caps.reserved_tokens == {"A": 2, "B": 1}
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
        assert status["caps"]["utilization"] == {
            "acceptedNodes": 3,
            "currentRound": 1,
            "runningNodes": 0,
            "queuedNodes": 0,
            "usedTokens": 3,
            "reservedTokens": 0,
            "remainingTokens": 0,
            "availableTokens": 0,
        }
        assert all(status["nodes"][node]["state"] == "succeeded" for node in effects)
        history_types = [event["type"] for event in status["history"]]
        assert history_types == [
            "approval",
            "validation",
            "run_queued",
            "run_running",
            "node_queued",
            "node_queued",
            "node_queued",
            "dispatch",
            "node_running",
            "dispatch",
            "node_running",
            "usage",
            "node_succeeded",
            "usage",
            "node_succeeded",
            "dispatch",
            "node_running",
            "usage",
            "node_succeeded",
            "run_succeeded",
        ]
        event_ids = [event["eventId"] for event in status["history"]]
        assert event_ids[: len(pre_crash_event_ids)] == pre_crash_event_ids
        assert len(event_ids) == len(set(event_ids))
        assert all(
            event["runId"] == run_id
            and event["timestamp"]
            and event["source"]
            and event["correlationKey"]
            for event in status["history"]
        )
        transitions = [
            (event["type"], event["nodeId"])
            for event in status["history"]
            if event["type"] in {"dispatch", "node_succeeded"}
        ]
        assert transitions.index(("node_succeeded", "A")) < transitions.index(("dispatch", "C"))
        assert transitions.index(("node_succeeded", "B")) < transitions.index(("dispatch", "C"))

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
        assert status["caps"]["utilization"] == {
            "acceptedNodes": 1,
            "currentRound": 1,
            "runningNodes": 0,
            "queuedNodes": 0,
            "usedTokens": 2,
            "reservedTokens": 0,
            "remainingTokens": 0,
            "availableTokens": 0,
        }
        history_types = [event["type"] for event in status["history"]]
        assert history_types == [
            "validation",
            "run_queued",
            "run_running",
            "node_queued",
            "dispatch",
            "node_running",
            "usage",
            "retry",
            "usage",
            "node_failed",
            "run_failed",
        ]
        retry = next(event for event in status["history"] if event["type"] == "retry")
        assert retry["nodeId"] == "FAIL"
        assert retry["metadata"] == {"attempt": 2}
        usage_events = [event for event in status["history"] if event["type"] == "usage"]
        assert [event["metadata"]["attempt"] for event in usage_events] == [1, 2]
        assert status["caps"]["utilization"]["usedTokens"] == sum(
            event["metadata"]["totalTokens"] for event in usage_events
        )

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


def test_durable_token_cap_denies_before_provider_effect() -> None:
    from dapr.clients import DaprClient
    from dapr.ext.workflow import DaprWorkflowClient

    from omnigent.flow.audit import DaprAuditStore
    from omnigent.flow.caps import DaprCapStore
    from omnigent.flow.usage import ConservativeUsagePolicy, DaprUsageStore, UsageService

    repo = Path(__file__).parents[2]
    process = _start(repo)
    try:
        _wait_until(lambda: all(readiness().values()))
        state_client = DaprClient()
        workflow_client = DaprWorkflowClient()
        run_id = f"flow-cap-denied-{uuid4()}"
        fixture = _denied_fixture(run_id)
        usage = UsageService(
            DaprUsageStore(state_client),
            missing_usage_policy=ConservativeUsagePolicy(1),
        )
        usage.record_attempt(
            run_id=run_id,
            idempotency_key=f"{run_id}:prior-usage",
            node_id="prior",
            attempt=1,
            provider="fake",
            model="deterministic",
            succeeded=True,
            usage=None,
            token_budget=1,
        )

        workflow_client.schedule_new_workflow(
            FLOW_WORKFLOW_NAME,
            input=fixture,
            instance_id=run_id,
        )
        completed = workflow_client.wait_for_workflow_completion(
            run_id,
            timeout_in_seconds=30,
        )
        assert completed is not None
        output = json.loads(completed.serialized_output)

        assert output["status"] == "failed"
        assert output["nodes"]["DENIED"]["failure"]["category"] == "budget"
        assert _effect(state_client, run_id, "DENIED") is None
        state = DaprCapStore(state_client).state(
            run_id,
            FlowWorkflowInput.model_validate(fixture).dag_spec.caps,
        )
        assert state.accepted_node_ids == ("DENIED",)
        assert state.current_round == 1
        assert state.running_node_ids == ()
        assert state.queued_node_ids == ()
        assert state.reserved_tokens == {}
        denial = next(
            event
            for event in DaprAuditStore(state_client).history(run_id)
            if event.type == "cap_denial"
        )
        assert denial.metadata == {
            "cap": "tokenBudget",
            "current": 1,
            "proposed": 2,
            "limit": 1,
        }
    finally:
        _graceful_stop(process)


def test_native_expansion_accepts_round_two_and_releases_every_reservation() -> None:
    from dapr.clients import DaprClient
    from dapr.ext.workflow import DaprWorkflowClient

    from omnigent.flow.audit import DaprAuditStore
    from omnigent.flow.caps import DaprCapStore
    from omnigent.flow.status import WorkflowStatusService
    from omnigent.flow.usage import ConservativeUsagePolicy, DaprUsageStore, UsageService

    repo = Path(__file__).parents[2]
    process = _start(repo, expansion_node="EXPAND")
    try:
        _wait_until(lambda: all(readiness().values()))
        state_client = DaprClient()
        workflow_client = DaprWorkflowClient()
        run_id = f"flow-expansion-{uuid4()}"
        fixture = _expansion_fixture(run_id)
        workflow_client.schedule_new_workflow(
            FLOW_WORKFLOW_NAME,
            input=fixture,
            instance_id=run_id,
        )
        completed = workflow_client.wait_for_workflow_completion(
            run_id,
            timeout_in_seconds=30,
        )
        assert completed is not None
        output = json.loads(completed.serialized_output)

        assert output["status"] == "succeeded"
        assert output["nodes"]["EXPAND"]["output"] == {"value": "EXPAND"}
        assert output["nodes"]["CHILD"]["output"] == {"values": ["EXPAND"]}
        expansion_events = [event for event in output["events"] if event["type"] == "expansion"]
        assert len(expansion_events) == 1
        assert expansion_events[0]["nodeId"] == "EXPAND"
        assert expansion_events[0]["round"] == 2

        effects = {
            node_id: _effect(state_client, run_id, node_id) for node_id in ("EXPAND", "CHILD")
        }
        assert all(effect["effectCount"] == 1 for effect in effects.values())
        assert all(effect["completed"] is True for effect in effects.values())

        limits = FlowWorkflowInput.model_validate(fixture).dag_spec.caps
        cap_state = DaprCapStore(state_client).state(run_id, limits)
        assert cap_state.accepted_node_ids == ("EXPAND", "CHILD")
        assert cap_state.current_round == 2
        assert cap_state.running_node_ids == ()
        assert cap_state.queued_node_ids == ()
        assert cap_state.reserved_tokens == {}

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
        assert status["caps"]["utilization"] == {
            "acceptedNodes": 2,
            "currentRound": 2,
            "runningNodes": 0,
            "queuedNodes": 0,
            "usedTokens": 2,
            "reservedTokens": 0,
            "remainingTokens": 0,
            "availableTokens": 0,
        }
        history_pairs = [(event["type"], event.get("nodeId")) for event in status["history"]]
        assert history_pairs.index(("node_succeeded", "EXPAND")) < history_pairs.index(
            ("expansion", "EXPAND")
        )
        assert history_pairs.index(("expansion", "EXPAND")) < history_pairs.index(
            ("dispatch", "CHILD")
        )
        assert history_pairs.index(("dispatch", "CHILD")) < history_pairs.index(
            ("node_succeeded", "CHILD")
        )
    finally:
        _graceful_stop(process)


@pytest.mark.asyncio
async def test_production_mcp_completes_and_survives_restart(tmp_path: Path) -> None:
    from dapr.ext.workflow import DaprWorkflowClient
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    repo = Path(__file__).parents[2]
    worker = _start(repo, slow_node=None, delay_seconds=0)
    approval_database = tmp_path / "approvals.sqlite3"
    distribution = tmp_path / "distribution"
    subprocess.run(
        ("uv", "build", "--wheel", "--out-dir", str(distribution)),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(distribution.glob("omnigent-*.whl"))
    assert len(wheels) == 1
    installed = tmp_path / "installed"
    subprocess.run(
        (
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(installed),
            "--no-deps",
            str(wheels[0]),
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    env = {
        **os.environ,
        "FLOW_MODE": "conformance",
        "FLOW_ACTOR": "e2e-operator",
        "FLOW_SIGNING_KEY": "native-e2e-production-signing-key",
        "FLOW_APPROVAL_DB": str(approval_database),
        "FLOW_APPROVAL_TTL_SECONDS": "300",
        "FLOW_DAPR_HEALTH_TIMEOUT_SECONDS": "5",
        "DAPR_GRPC_PORT": "50101",
        "DAPR_HTTP_PORT": "3510",
        "PYTHONPATH": str(installed),
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "omnigent.flow.mcp_server"],
        cwd=tmp_path,
        env=env,
    )

    def structured(result) -> dict:
        assert isinstance(result.structuredContent, dict)
        return result.structuredContent

    try:
        _wait_until(lambda: all(readiness().values()))
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert [tool.name for tool in tools.tools] == [
                    "propose_dag",
                    "run_workflow",
                    "get_workflow_status",
                    "list_workflows",
                ]
                proposal = structured(
                    await session.call_tool(
                        "propose_dag",
                        {"task_description": "Execute the shared three-node fixture"},
                    )
                )
                preview = structured(
                    await session.call_tool(
                        "run_workflow",
                        {
                            "dag_spec": proposal["dagSpec"],
                            "confirm": False,
                            "idempotency_key": "native-request-1",
                        },
                    )
                )
                confirmation = {
                    "dag_spec": proposal["dagSpec"],
                    "approval_token": preview["approvalToken"],
                    "confirm": True,
                    "idempotency_key": "native-request-1",
                }
                started = structured(await session.call_tool("run_workflow", confirmation))
                run_id = started["runId"]
                observer = DaprWorkflowClient()
                try:
                    completed = await asyncio.to_thread(
                        observer.wait_for_workflow_completion,
                        run_id,
                        timeout_in_seconds=30,
                    )
                finally:
                    observer.close()
                assert completed is not None
                status = structured(
                    await session.call_tool("get_workflow_status", {"run_id": run_id})
                )
                listed = structured(
                    await session.call_tool(
                        "list_workflows",
                        {"created_after": started["createdAt"], "limit": 100},
                    )
                )

                assert status["state"] == "succeeded"
                assert status["caps"]["utilization"] == {
                    "acceptedNodes": 3,
                    "currentRound": 1,
                    "runningNodes": 0,
                    "queuedNodes": 0,
                    "usedTokens": 3,
                    "reservedTokens": 0,
                    "remainingTokens": 0,
                    "availableTokens": 0,
                }
                assert len([event for event in status["history"] if event["type"] == "usage"]) == 3
                listed_run = next(item for item in listed["workflows"] if item["runId"] == run_id)
                assert listed_run["state"] == "succeeded"
                assert "not_implemented" not in json.dumps(
                    [proposal, preview, started, status, listed]
                )

        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as restarted_session:
                await restarted_session.initialize()
                replayed = structured(
                    await restarted_session.call_tool("run_workflow", confirmation)
                )
                restarted_status = structured(
                    await restarted_session.call_tool("get_workflow_status", {"run_id": run_id})
                )
                restarted_list = structured(
                    await restarted_session.call_tool(
                        "list_workflows",
                        {"created_after": started["createdAt"], "limit": 100},
                    )
                )

                assert replayed["runId"] == run_id
                assert replayed["reused"] is True
                assert restarted_status["state"] == "succeeded"
                restarted_run = next(
                    item for item in restarted_list["workflows"] if item["runId"] == run_id
                )
                assert restarted_run["state"] == "succeeded"
    finally:
        _graceful_stop(worker)
