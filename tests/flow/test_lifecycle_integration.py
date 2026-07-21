from dataclasses import dataclass
from datetime import UTC, datetime

from dapr.ext.workflow.workflow_state import WorkflowStatus

from omnigent.flow.audit import InMemoryAuditStore
from omnigent.flow.lifecycle import LifecycleService


@dataclass
class State:
    runtime_status: WorkflowStatus


class DaprBoundary:
    """Records the exact Dapr Workflow 1.18.0 client calls used by the service."""

    def __init__(self) -> None:
        self.state = State(WorkflowStatus.RUNNING)
        self.calls: list[tuple[str, object]] = []

    def get_workflow_state(self, instance_id: str) -> State:
        self.calls.append(("get_workflow_state", instance_id))
        return self.state

    def pause_workflow(self, instance_id: str) -> None:
        self.calls.append(("pause_workflow", instance_id))
        self.state = State(WorkflowStatus.SUSPENDED)

    def resume_workflow(self, instance_id: str) -> None:
        self.calls.append(("resume_workflow", instance_id))
        self.state = State(WorkflowStatus.RUNNING)

    def terminate_workflow(
        self,
        instance_id: str,
        *,
        output: object | None = None,
        recursive: bool = True,
    ) -> None:
        self.calls.append(("terminate_workflow", (instance_id, output, recursive)))
        self.state = State(WorkflowStatus.TERMINATED)


def test_service_uses_only_versioned_dapr_lifecycle_methods() -> None:
    client = DaprBoundary()
    controls = LifecycleService(
        client,
        audit=InMemoryAuditStore(),
        authorizer=lambda _actor, _action, _run_id: True,
        clock=lambda: datetime(2026, 7, 21, tzinfo=UTC),
    )

    controls.pause("run-1", actor="operator")
    controls.resume("run-1", actor="operator")
    controls.cancel("run-1", actor="operator")

    assert [name for name, _ in client.calls] == [
        "get_workflow_state",
        "pause_workflow",
        "get_workflow_state",
        "resume_workflow",
        "get_workflow_state",
        "terminate_workflow",
    ]
    assert client.calls[-1][1] == (
        "run-1",
        {"reason": "canceled"},
        True,
    )
