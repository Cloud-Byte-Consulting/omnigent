import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from dapr.ext.workflow.workflow_state import WorkflowStatus

from omnigent.flow import composition
from omnigent.flow.composition import (
    FlowApplicationConfig,
    build_flow_application,
)


@dataclass
class StateResponse:
    data: bytes
    etag: str


class FakeStateClient:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], tuple[bytes, int]] = {}
        self.closed = False
        self.fail_catalog = False

    def get_state(self, store_name: str, key: str) -> StateResponse:
        data, version = self.values.get((store_name, key), (b"", 0))
        return StateResponse(data, str(version) if version else "")

    def save_state(self, store_name, key, value, *, etag, options):
        del options
        if self.fail_catalog and key == "flow-workflow-index":
            raise RuntimeError("catalog unavailable")
        _data, version = self.values.get((store_name, key), (b"", 0))
        expected = str(version) if version else None
        if etag != expected:
            raise RuntimeError("etag conflict")
        encoded = value.encode() if isinstance(value, str) else value
        self.values[(store_name, key)] = (encoded, version + 1)

    def close(self) -> None:
        self.closed = True


@dataclass
class WorkflowState:
    runtime_status: WorkflowStatus
    created_at: datetime
    last_updated_at: datetime
    serialized_input: str
    serialized_output: str
    serialized_custom_status: str


class FakeWorkflowClient:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.states: dict[str, WorkflowState] = {}
        self.closed = False
        self.schedule_calls = 0
        self.fail_close = False

    def schedule_new_workflow(
        self,
        workflow: str,
        *,
        input: dict[str, Any],
        instance_id: str,
    ) -> None:
        assert workflow == "FlowDagWorkflow"
        self.schedule_calls += 1
        self.states[instance_id] = WorkflowState(
            WorkflowStatus.PENDING,
            self.now,
            self.now,
            json.dumps(input),
            "",
            "",
        )

    def get_workflow_state(self, instance_id: str) -> WorkflowState | None:
        return self.states.get(instance_id)

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("close failed")


def config(database: Path) -> FlowApplicationConfig:
    return FlowApplicationConfig(
        mode="conformance",
        actor="operator",
        signing_key=b"production-composition-test-key",
        approval_database=database,
        approval_ttl=timedelta(minutes=5),
        dapr_grpc_port=50101,
        dapr_http_port=3510,
        dapr_health_timeout_seconds=0.1,
    )


@pytest.mark.asyncio
async def test_composed_service_connects_all_four_tools_without_test_imports(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 21, tzinfo=UTC)
    state_client = FakeStateClient()
    workflow_client = FakeWorkflowClient(now)
    identifiers = iter(("approval-1", "run-1", "unused"))
    application = build_flow_application(
        config(tmp_path / "approvals.sqlite3"),
        state_client=state_client,
        workflow_client=workflow_client,
        clock=lambda: now,
        id_factory=lambda: next(identifiers),
    )

    proposal = await application.service.propose_dag("Run the shared conformance flow")
    assert proposal["status"] == "proposed"
    dag = proposal["dagSpec"]
    preview = await application.service.run_workflow(
        dag,
        confirm=False,
        idempotency_key="request-1",
    )
    started = await application.service.run_workflow(
        dag,
        approval_token=preview["approvalToken"],
        confirm=True,
        idempotency_key="request-1",
    )
    status = await application.service.get_workflow_status(started["runId"])
    listed = await application.service.list_workflows(None, None, 20)

    assert [node["id"] for node in dag["nodes"]] == ["A", "B", "C"]
    assert preview["status"] == "approval_required"
    assert started["state"] == "queued"
    assert status["state"] == "queued"
    assert listed["workflows"][0]["runId"] == started["runId"]
    assert "not_implemented" not in json.dumps([proposal, preview, started, status, listed])

    application.close()
    assert state_client.closed is True
    assert workflow_client.closed is True


@pytest.mark.asyncio
async def test_catalog_failure_retries_the_same_run_without_duplicate_schedule(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 21, tzinfo=UTC)
    state_client = FakeStateClient()
    workflow_client = FakeWorkflowClient(now)
    identifiers = iter(("approval-1", "unused"))
    application = build_flow_application(
        config(tmp_path / "approvals.sqlite3"),
        state_client=state_client,
        workflow_client=workflow_client,
        clock=lambda: now,
        id_factory=lambda: next(identifiers),
    )
    proposal = await application.service.propose_dag("Recover catalog persistence")
    dag = proposal["dagSpec"]
    preview = await application.service.run_workflow(
        dag, confirm=False, idempotency_key="request-1"
    )
    arguments = {
        "dag_spec": dag,
        "approval_token": preview["approvalToken"],
        "confirm": True,
        "idempotency_key": "request-1",
    }

    state_client.fail_catalog = True
    with pytest.raises(RuntimeError, match="catalog unavailable"):
        await application.service.run_workflow(**arguments)
    state_client.fail_catalog = False
    recovered = await application.service.run_workflow(**arguments)

    assert recovered["state"] == "queued"
    assert workflow_client.schedule_calls == 1
    assert list(workflow_client.states) == [recovered["runId"]]
    application.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "constraints",
    [
        {"allowedModels": ["openai:gpt-5"]},
        {"allowedTools": ["search"]},
    ],
)
async def test_conformance_mode_clarifies_unsupported_provider_capabilities(
    tmp_path: Path,
    constraints: dict[str, list[str]],
) -> None:
    application = build_flow_application(
        config(tmp_path / "approvals.sqlite3"),
        state_client=FakeStateClient(),
        workflow_client=FakeWorkflowClient(datetime(2026, 7, 21, tzinfo=UTC)),
    )

    result = await application.service.propose_dag("Unsupported route", constraints)

    assert result["status"] == "clarification_required"
    assert "dagSpec" not in result
    application.close()


def test_application_closes_all_clients_when_one_close_fails(tmp_path: Path) -> None:
    state_client = FakeStateClient()
    workflow_client = FakeWorkflowClient(datetime(2026, 7, 21, tzinfo=UTC))
    workflow_client.fail_close = True
    application = build_flow_application(
        config(tmp_path / "approvals.sqlite3"),
        state_client=state_client,
        workflow_client=workflow_client,
    )

    application.close()
    application.close()

    assert workflow_client.closed is True
    assert state_client.closed is True


def test_partial_client_construction_failure_closes_acquired_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_client = FakeStateClient()
    monkeypatch.setattr(composition, "_require_dapr_ready", lambda _config: None)
    monkeypatch.setattr(composition, "DaprClient", lambda **_kwargs: state_client)

    def fail_workflow_client(**_kwargs):
        raise RuntimeError("workflow client construction failed")

    monkeypatch.setattr(composition, "DaprWorkflowClient", fail_workflow_client)

    with pytest.raises(RuntimeError, match="workflow client construction failed"):
        build_flow_application(config(tmp_path / "approvals.sqlite3"))
    assert state_client.closed is True


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"FLOW_MODE": None}, "FLOW_MODE is required"),
        ({"FLOW_ACTOR": None}, "FLOW_ACTOR is required"),
        ({"FLOW_SIGNING_KEY": "short"}, "FLOW_SIGNING_KEY must contain at least 16 bytes"),
        ({"DAPR_GRPC_PORT": None}, "DAPR_GRPC_PORT is required"),
        ({"DAPR_HTTP_PORT": "secret-value"}, "DAPR_HTTP_PORT must be a TCP port"),
    ],
)
def test_configuration_fails_closed_without_echoing_values(
    tmp_path: Path,
    changes: dict[str, str | None],
    message: str,
) -> None:
    values: dict[str, str] = {
        "FLOW_MODE": "conformance",
        "FLOW_ACTOR": "operator",
        "FLOW_SIGNING_KEY": "a-valid-signing-key",
        "FLOW_APPROVAL_DB": str(tmp_path / "approvals.sqlite3"),
        "FLOW_APPROVAL_TTL_SECONDS": "300",
        "DAPR_GRPC_PORT": "50101",
        "DAPR_HTTP_PORT": "3510",
    }
    for key, value in changes.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value

    with pytest.raises(ValueError, match=message) as caught:
        FlowApplicationConfig.from_env(values)
    assert "secret-value" not in str(caught.value)
