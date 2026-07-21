from dataclasses import dataclass
from datetime import UTC, datetime

from dapr.ext.workflow.workflow_state import WorkflowStatus

from omnigent.flow.audit import InMemoryAuditStore
from omnigent.flow.lifecycle import LifecycleService


@dataclass
class State:
    runtime_status: WorkflowStatus


class FakeClient:
    def __init__(self, status: WorkflowStatus | None = WorkflowStatus.RUNNING) -> None:
        self.status = status
        self.calls: list[tuple[str, str, object | None]] = []

    def get_workflow_state(self, instance_id: str) -> State | None:
        self.calls.append(("get", instance_id, None))
        return None if self.status is None else State(self.status)

    def pause_workflow(self, instance_id: str) -> None:
        self.calls.append(("pause", instance_id, None))
        self.status = WorkflowStatus.SUSPENDED

    def resume_workflow(self, instance_id: str) -> None:
        self.calls.append(("resume", instance_id, None))
        self.status = WorkflowStatus.RUNNING

    def terminate_workflow(
        self,
        instance_id: str,
        *,
        output: object | None = None,
        recursive: bool = True,
    ) -> None:
        assert recursive is True
        self.calls.append(("terminate", instance_id, output))
        self.status = WorkflowStatus.TERMINATED


def service(
    client: FakeClient,
    *,
    authorized: bool = True,
) -> tuple[LifecycleService, InMemoryAuditStore]:
    audit = InMemoryAuditStore()
    return (
        LifecycleService(
            client,
            audit=audit,
            authorizer=lambda actor, action, run_id: authorized
            and actor == "operator"
            and bool(action)
            and bool(run_id),
            clock=lambda: datetime(2026, 7, 21, tzinfo=UTC),
        ),
        audit,
    )


def test_authorized_active_run_can_be_paused_without_scheduling_work() -> None:
    client = FakeClient()
    controls, audit = service(client)

    result = controls.pause("run-1", actor="operator")

    assert result.to_dict() == {
        "runId": "run-1",
        "action": "pause",
        "status": "paused",
        "changed": True,
        "error": None,
    }
    assert [call[0] for call in client.calls] == ["get", "pause"]
    assert [event.type for event in audit.history("run-1")] == ["pause"]


def test_paused_run_resumes_without_any_node_dispatch_api() -> None:
    client = FakeClient(WorkflowStatus.SUSPENDED)
    controls, audit = service(client)

    result = controls.resume("run-1", actor="operator")

    assert result.status == "running"
    assert result.changed is True
    assert [call[0] for call in client.calls] == ["get", "resume"]
    assert [event.type for event in audit.history("run-1")] == ["resume"]
    assert not hasattr(client, "schedule_new_workflow")


def test_cancel_is_idempotent_and_returns_the_same_terminal_status() -> None:
    client = FakeClient()
    controls, audit = service(client)

    first = controls.cancel("run-1", actor="operator")
    second = controls.cancel("run-1", actor="operator")

    assert first.status == second.status == "canceled"
    assert first.changed is True
    assert second.changed is False
    assert [call[0] for call in client.calls].count("terminate") == 1
    assert len(audit.history("run-1")) == 1
    assert audit.history("run-1")[0].type == "cancel"


def test_pause_or_resume_terminal_run_returns_conflict_without_mutation() -> None:
    for action in ("pause", "resume"):
        client = FakeClient(WorkflowStatus.COMPLETED)
        controls, audit = service(client)

        result = getattr(controls, action)("run-1", actor="operator")

        assert result.status == "succeeded"
        assert result.changed is False
        assert result.error == "conflict"
        assert [call[0] for call in client.calls] == ["get"]
        assert audit.history("run-1")[0].type == "denial"


def test_unknown_dapr_state_is_never_mutated() -> None:
    client = FakeClient(WorkflowStatus.UNKNOWN)
    controls, audit = service(client)

    result = controls.cancel("run-1", actor="operator")

    assert result.status == "unknown"
    assert result.error == "conflict"
    assert [call[0] for call in client.calls] == ["get"]
    assert audit.history("run-1")[0].type == "denial"


def test_unauthorized_or_missing_run_never_mutates_dapr() -> None:
    unauthorized = FakeClient()
    controls, audit = service(unauthorized, authorized=False)

    forbidden = controls.cancel("run-1", actor="intruder")

    assert forbidden.error == "forbidden"
    assert unauthorized.calls == []
    assert audit.history("run-1")[0].type == "denial"

    missing = FakeClient(None)
    controls, _audit = service(missing)
    not_found = controls.pause("missing", actor="operator")
    assert not_found.error == "not_found"
    assert [call[0] for call in missing.calls] == ["get"]
