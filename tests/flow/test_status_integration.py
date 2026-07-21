import json
from dataclasses import dataclass
from datetime import UTC, datetime

from dapr.ext.workflow.workflow_state import WorkflowStatus

from omnigent.flow.audit import InMemoryAuditStore
from omnigent.flow.caps import InMemoryCapStore
from omnigent.flow.status import WorkflowStatusService
from omnigent.flow.usage import ConservativeUsagePolicy, InMemoryUsageStore, UsageService


@dataclass
class DaprWorkflowStateBoundary:
    runtime_status: WorkflowStatus = WorkflowStatus.SUSPENDED
    created_at: datetime = datetime(2026, 7, 21, tzinfo=UTC)
    last_updated_at: datetime = datetime(2026, 7, 21, 0, 1, tzinfo=UTC)
    serialized_input: str = json.dumps(
        {
            "runId": "run-1",
            "approvedDagDigest": "digest",
            "dagSpec": {
                "version": "1.0",
                "nodes": [{"id": "A", "instructions": "A", "model": "fake:alpha"}],
                "caps": {
                    "maxNodes": 1,
                    "maxRounds": 1,
                    "maxConcurrent": 1,
                    "tokenBudget": 10,
                },
            },
        }
    )
    serialized_output: str | None = None
    serialized_custom_status: str | None = json.dumps(
        {"status": "paused", "nodes": {"A": {"status": "queued"}}, "events": []}
    )


class Client:
    def get_workflow_state(self, instance_id: str) -> DaprWorkflowStateBoundary | None:
        return DaprWorkflowStateBoundary() if instance_id == "run-1" else None


def test_dapr_status_boundary_composes_a_json_safe_paused_view() -> None:
    service = WorkflowStatusService(
        Client(),
        audit=InMemoryAuditStore(),
        usage=UsageService(
            InMemoryUsageStore(),
            missing_usage_policy=ConservativeUsagePolicy(5),
        ),
        caps=InMemoryCapStore(),
        authorizer=lambda _actor, _run_id: True,
    )

    result = service.get("run-1", actor="operator")

    assert result["state"] == "paused"
    assert result["nodes"]["A"]["state"] == "queued"
    json.dumps(result)
