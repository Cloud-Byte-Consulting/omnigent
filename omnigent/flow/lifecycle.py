"""Authorized, audited lifecycle controls over Dapr Workflow."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, TypeAlias

from dapr.ext.workflow.workflow_state import WorkflowStatus

from omnigent.flow.audit import AuditEvent, create_audit_event

LifecycleAction: TypeAlias = Literal["pause", "resume", "cancel"]
RunStatus: TypeAlias = Literal[
    "queued",
    "running",
    "paused",
    "succeeded",
    "failed",
    "canceled",
    "unknown",
]
LifecycleError: TypeAlias = Literal["forbidden", "not_found", "conflict"]


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    """Canonical result returned by every lifecycle request."""

    run_id: str
    action: LifecycleAction
    status: RunStatus
    changed: bool
    error: LifecycleError | None

    def to_dict(self) -> dict[str, object]:
        return {
            "runId": self.run_id,
            "action": self.action,
            "status": self.status,
            "changed": self.changed,
            "error": self.error,
        }


class WorkflowState(Protocol):
    runtime_status: WorkflowStatus


class WorkflowClient(Protocol):
    """Exact Dapr Workflow 1.18.0 methods used by lifecycle controls."""

    def get_workflow_state(self, instance_id: str) -> WorkflowState | None: ...

    def pause_workflow(self, instance_id: str) -> object: ...

    def resume_workflow(self, instance_id: str) -> object: ...

    def terminate_workflow(
        self,
        instance_id: str,
        *,
        output: object | None = None,
        recursive: bool = True,
    ) -> object: ...


class AuditStore(Protocol):
    def append(self, event: AuditEvent) -> AuditEvent: ...


Authorizer = Callable[[str, LifecycleAction, str], bool]


class LifecycleService:
    """Apply authorized state transitions without scheduling workflow work."""

    def __init__(
        self,
        client: WorkflowClient,
        *,
        audit: AuditStore,
        authorizer: Authorizer,
        clock: Callable[[], datetime],
    ) -> None:
        self._client = client
        self._audit_store = audit
        self._authorizer = authorizer
        self._clock = clock

    def pause(self, run_id: str, *, actor: str) -> LifecycleResult:
        return self._apply(run_id, "pause", actor)

    def resume(self, run_id: str, *, actor: str) -> LifecycleResult:
        return self._apply(run_id, "resume", actor)

    def cancel(self, run_id: str, *, actor: str) -> LifecycleResult:
        return self._apply(run_id, "cancel", actor)

    def _apply(
        self,
        run_id: str,
        action: LifecycleAction,
        actor: str,
    ) -> LifecycleResult:
        if not run_id or not actor or not self._authorizer(actor, action, run_id):
            result = LifecycleResult(run_id, action, "unknown", False, "forbidden")
            self._audit(result, actor)
            return result

        state = self._client.get_workflow_state(run_id)
        if state is None:
            result = LifecycleResult(run_id, action, "unknown", False, "not_found")
            self._audit(result, actor)
            return result
        status = _flow_status(state.runtime_status)

        result = self._transition(run_id, action, status)
        self._audit(result, actor)
        return result

    def _transition(
        self,
        run_id: str,
        action: LifecycleAction,
        status: RunStatus,
    ) -> LifecycleResult:
        if action == "pause":
            if status == "paused":
                return LifecycleResult(run_id, action, status, False, None)
            if status not in {"queued", "running"}:
                return LifecycleResult(run_id, action, status, False, "conflict")
            self._client.pause_workflow(run_id)
            return LifecycleResult(run_id, action, "paused", True, None)

        if action == "resume":
            if status == "running":
                return LifecycleResult(run_id, action, status, False, None)
            if status != "paused":
                return LifecycleResult(run_id, action, status, False, "conflict")
            self._client.resume_workflow(run_id)
            return LifecycleResult(run_id, action, "running", True, None)

        if status == "canceled":
            return LifecycleResult(run_id, action, status, False, None)
        if status in {"succeeded", "failed", "unknown"}:
            return LifecycleResult(run_id, action, status, False, "conflict")
        self._client.terminate_workflow(
            run_id,
            output={"reason": "canceled"},
            recursive=True,
        )
        return LifecycleResult(run_id, action, "canceled", True, None)

    def _audit(self, result: LifecycleResult, actor: str) -> None:
        accepted = result.error is None
        event_type = result.action if accepted else "denial"
        summary = (
            f"Run lifecycle action {result.action} accepted"
            if accepted
            else f"Run lifecycle action {result.action} denied"
        )
        self._audit_store.append(
            create_audit_event(
                run_id=result.run_id or "unknown",
                node_id=None,
                event_type=event_type,
                timestamp=self._clock(),
                source="lifecycle_control",
                correlation_key=(
                    f"lifecycle:{result.run_id}:{result.action}:{result.status}:{result.error}"
                ),
                summary=summary,
                metadata={
                    "action": result.action,
                    "actor": actor,
                    "status": result.status,
                    "changed": result.changed,
                    "error": result.error,
                },
            )
        )


def _flow_status(status: WorkflowStatus) -> RunStatus:
    mapping: dict[WorkflowStatus, RunStatus] = {
        WorkflowStatus.PENDING: "queued",
        WorkflowStatus.RUNNING: "running",
        WorkflowStatus.SUSPENDED: "paused",
        WorkflowStatus.COMPLETED: "succeeded",
        WorkflowStatus.FAILED: "failed",
        WorkflowStatus.STALLED: "failed",
        WorkflowStatus.TERMINATED: "canceled",
        WorkflowStatus.UNKNOWN: "unknown",
    }
    return mapping[status]
