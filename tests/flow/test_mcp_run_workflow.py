from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnigent.flow.approval import ApprovalService, InMemoryApprovalStore
from omnigent.flow.mcp_run import ApprovedDaprWorkflowStarter, WorkflowRunFlowService
from omnigent.flow.mcp_server import create_server
from omnigent.flow.mcp_status import StatusFlowService

NOW = datetime(2026, 7, 21, tzinfo=UTC)


def dag(**changes: Any) -> dict[str, Any]:
    value = {
        "version": "1.0",
        "defaultModel": "fake:deterministic",
        "nodes": [{"id": "A", "instructions": "Run A"}],
        "caps": {
            "maxNodes": 1,
            "maxRounds": 1,
            "maxConcurrent": 1,
            "tokenBudget": 10,
        },
    }
    value.update(changes)
    return value


class WorkflowClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def schedule_new_workflow(self, workflow, *, input, instance_id):
        self.calls.append({"workflow": workflow, "input": input, "instanceId": instance_id})


class EmptyStatus:
    def get(self, run_id: str, *, actor: str) -> dict[str, object]:
        return {"runId": run_id, "actor": actor}


def application(now=lambda: NOW):
    store = InMemoryApprovalStore()
    client = WorkflowClient()
    ids = iter(("approval-1", "run-1", "unused"))
    approvals = ApprovalService(
        store,
        signing_key=b"run-workflow-signing-key",
        start_run=ApprovedDaprWorkflowStarter(client),
        id_factory=lambda: next(ids),
    )
    fallback = StatusFlowService(EmptyStatus(), actor="operator")
    return (
        WorkflowRunFlowService(
            approvals,
            actor="operator",
            clock=now,
            approval_ttl=timedelta(minutes=5),
            fallback=fallback,
        ),
        client,
    )


async def test_preview_requires_approval_without_creating_a_run() -> None:
    service, client = application()

    result = await service.run_workflow(dag(), confirm=False, idempotency_key="request-1")

    assert result["status"] == "approval_required"
    assert result["dagDigest"] == result["preview"]["digest"]
    assert result["approvalToken"]
    assert result["approvalExpiresAt"] == (NOW + timedelta(minutes=5)).isoformat()
    approved_dag = result["preview"]["dag"]
    assert approved_dag["nodes"][0]["id"] == "A"
    assert approved_dag["nodes"][0]["canExpand"] is False
    assert client.calls == []


async def test_confirmed_exact_revision_starts_once_and_retry_reuses_run() -> None:
    service, client = application()
    preview = await service.run_workflow(dag(), confirm=False, idempotency_key="request-1")

    first = await service.run_workflow(
        dag(),
        approval_token=preview["approvalToken"],
        confirm=True,
        idempotency_key="request-1",
    )
    replay = await service.run_workflow(
        dag(),
        approval_token=preview["approvalToken"],
        confirm=True,
        idempotency_key="request-1",
    )

    assert first["runId"] == replay["runId"] == "run-1"
    assert first["state"] == replay["state"] == "queued"
    assert (first["reused"], replay["reused"]) == (False, True)
    assert len(client.calls) == 1
    assert client.calls[0]["workflow"] == "FlowDagWorkflow"
    assert client.calls[0]["instanceId"] == "run-1"
    assert client.calls[0]["input"]["dagSpec"] == preview["preview"]["dag"]
    assert client.calls[0]["input"]["approvedDagDigest"] == preview["dagDigest"]


@pytest.mark.parametrize("condition", ["missing", "stale", "expired", "wrong-key"])
async def test_unsafe_confirmation_never_creates_a_run(condition: str) -> None:
    current = NOW
    service, client = application(now=lambda: current)
    preview = await service.run_workflow(dag(), confirm=False, idempotency_key="request-1")
    token = None if condition == "missing" else preview["approvalToken"]
    candidate = dag()
    key = "request-2" if condition == "wrong-key" else "request-1"
    if condition == "stale":
        candidate["nodes"][0]["instructions"] = "changed"
    if condition == "expired":
        current = NOW + timedelta(minutes=6)

    result = await service.run_workflow(
        candidate,
        approval_token=token,
        confirm=True,
        idempotency_key=key,
    )

    assert result["error"]["code"] in {"missing_approval", "approval_invalid"}
    assert client.calls == []


async def test_fastmcp_run_schema_and_composable_status_boundary() -> None:
    service, _client = application()
    server = create_server(service)

    _content, preview = await server.call_tool(
        "run_workflow",
        {"dag_spec": dag(), "confirm": False, "idempotency_key": "request-1"},
    )
    _status_content, status = await server.call_tool(
        "get_workflow_status", {"run_id": "run-else"}
    )

    assert preview["status"] == "approval_required"
    assert status == {"runId": "run-else", "actor": "operator"}
