"""Self-contained, redacted status composition for Flow runs."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Protocol, TypeAlias, cast

from dapr.ext.workflow.workflow_state import WorkflowStatus

from omnigent.flow.audit import AuditEvent
from omnigent.flow.caps import CapState, CapStore
from omnigent.flow.orchestration import FlowWorkflowInput
from omnigent.flow.usage import RunUsageState, UsageRecord, UsageService

JsonObject: TypeAlias = dict[str, Any]


class DaprWorkflowState(Protocol):
    runtime_status: WorkflowStatus
    created_at: datetime
    last_updated_at: datetime
    serialized_input: object
    serialized_output: object
    serialized_custom_status: object


class WorkflowClient(Protocol):
    def get_workflow_state(self, instance_id: str) -> DaprWorkflowState | None: ...


class AuditStore(Protocol):
    def history(self, run_id: str) -> tuple[AuditEvent, ...]: ...


Authorizer = Callable[[str, str], bool]


class WorkflowStatusService:
    """Compose Dapr, audit, usage, and cap state into one safe JSON view."""

    def __init__(
        self,
        client: WorkflowClient,
        *,
        audit: AuditStore,
        usage: UsageService,
        caps: CapStore,
        authorizer: Authorizer,
    ) -> None:
        self._client = client
        self._audit = audit
        self._usage = usage
        self._caps = caps
        self._authorizer = authorizer

    def get(self, run_id: str, *, actor: str) -> JsonObject:
        """Return not_found for both missing and unauthorized run identities."""
        if not run_id or not actor or not self._authorizer(actor, run_id):
            return _not_found(run_id)
        state = self._client.get_workflow_state(run_id)
        if state is None:
            return _not_found(run_id)

        workflow_input = FlowWorkflowInput.model_validate(_decode_object(state.serialized_input))
        if workflow_input.run_id != run_id:
            return _not_found(run_id)
        dag = workflow_input.dag_spec
        history = self._audit.history(run_id)
        usage = self._usage.state(run_id, token_budget=dag.caps.token_budget)
        cap_state = self._caps.state(run_id, dag.caps)
        output = _optional_object(state.serialized_output)
        custom_status = _optional_object(state.serialized_custom_status)
        node_state_source = _node_state_source(output, custom_status)
        run_state = _run_state(state.runtime_status, output, custom_status)

        result: JsonObject = {
            "runId": run_id,
            "dag": {
                "digest": workflow_input.approved_dag_digest,
                "version": dag.version,
            },
            "state": run_state,
            "timestamps": _run_timestamps(state, history, run_state),
            "approval": _approval_summary(history),
            "caps": _cap_summary(cap_state, usage),
            "defaultModel": dag.default_model,
            "nodes": {
                node.id: _node_summary(
                    node.id,
                    list(node.depends_on),
                    node.model or dag.default_model,
                    node_state_source.get(node.id, {}),
                    history,
                    usage.records,
                )
                for node in dag.nodes
            },
            "history": [event.to_dict() for event in history],
            "expansionHistory": [
                event.to_dict()
                for event in history
                if event.type == "expansion"
                or (event.type == "cap_denial" and "round" in event.metadata)
            ],
            "pauseReason": _latest_summary(history, {"pause"}) if run_state == "paused" else None,
            "cancelReason": (
                _latest_summary(history, {"cancel"}) if run_state == "canceled" else None
            ),
            "interventionReason": _intervention_reason(history, run_state),
            "redaction": {
                "credentialsExcluded": True,
                "rawProviderPayloadsExcluded": True,
                "sensitiveValuesRedacted": True,
            },
        }
        return result


def _node_summary(
    node_id: str,
    dependencies: list[str],
    model_reference: str | None,
    raw_state: object,
    history: tuple[AuditEvent, ...],
    usage_records: tuple[UsageRecord, ...],
) -> JsonObject:
    state = raw_state if isinstance(raw_state, Mapping) else {}
    node_events = tuple(event for event in history if event.node_id == node_id)
    records = tuple(record for record in usage_records if record.node_id == node_id)
    status = state.get("status") if isinstance(state.get("status"), str) else "queued"
    attempts = max(
        [
            *(record.attempt for record in records),
            *(
                cast(int, event.metadata["attempt"])
                for event in node_events
                if isinstance(event.metadata.get("attempt"), int)
            ),
            cast(int, state.get("attempt", 0)) if isinstance(state.get("attempt", 0), int) else 0,
        ],
        default=0,
    )
    failure = _safe_failure(state.get("failure"))
    provider, model = _model_parts(model_reference)
    return {
        "id": node_id,
        "dependencies": dependencies,
        "state": status,
        "attempts": attempts,
        "timestamps": _node_timestamps(node_events),
        "blockedBy": _string_list(state.get("blockedBy")),
        "failure": failure,
        "validatedResultAvailable": status == "succeeded",
        "usage": _node_usage(records),
        "provider": provider,
        "model": model,
    }


def _safe_failure(value: object) -> JsonObject | None:
    if not isinstance(value, Mapping):
        return None
    category = value.get("category")
    message = value.get("message")
    retryable = value.get("retryable") is True
    return {
        "category": category if isinstance(category, str) else "permanent",
        "retryable": retryable,
        "message": message if isinstance(message, str) else "node execution failed",
        "policyExhausted": not retryable,
    }


def _node_usage(records: tuple[UsageRecord, ...]) -> JsonObject:
    input_values = [record.input_tokens for record in records if record.input_tokens is not None]
    output_values = [
        record.output_tokens for record in records if record.output_tokens is not None
    ]
    return {
        "inputTokens": sum(input_values) if input_values else None,
        "outputTokens": sum(output_values) if output_values else None,
        "totalTokens": sum(record.total_tokens for record in records),
        "estimated": any(record.estimated for record in records),
    }


def _cap_summary(state: CapState, usage: RunUsageState) -> JsonObject:
    reserved_tokens = sum(state.reserved_tokens.values())
    return {
        "limits": state.limits.model_dump(mode="json", by_alias=True),
        "utilization": {
            "acceptedNodes": len(state.accepted_node_ids),
            "currentRound": state.current_round,
            "runningNodes": len(state.running_node_ids),
            "queuedNodes": len(state.queued_node_ids),
            "usedTokens": usage.used_tokens,
            "reservedTokens": reserved_tokens,
            "remainingTokens": usage.remaining_tokens,
            "availableTokens": max(usage.remaining_tokens - reserved_tokens, 0),
        },
    }


def _approval_summary(history: tuple[AuditEvent, ...]) -> JsonObject:
    event = next((item for item in reversed(history) if item.type == "approval"), None)
    if event is None:
        return {"approved": False, "decision": None, "approver": None, "decidedAt": None}
    decision = event.metadata.get("decision")
    approver = event.metadata.get("approver")
    return {
        "approved": decision == "approved",
        "decision": decision if isinstance(decision, str) else None,
        "approver": approver if isinstance(approver, str) else None,
        "decidedAt": event.timestamp.isoformat(),
    }


def _run_timestamps(
    state: DaprWorkflowState,
    history: tuple[AuditEvent, ...],
    run_state: str,
) -> JsonObject:
    started = next((event for event in history if event.type == "run_running"), None)
    terminal = next(
        (
            event
            for event in reversed(history)
            if event.type in {"run_succeeded", "run_failed", "run_canceled", "run_rejected"}
        ),
        None,
    )
    return {
        "createdAt": _iso(state.created_at),
        "startedAt": started.timestamp.isoformat() if started else None,
        "updatedAt": _iso(state.last_updated_at),
        "completedAt": (
            terminal.timestamp.isoformat()
            if terminal
            else _iso(state.last_updated_at)
            if run_state in {"succeeded", "failed", "canceled", "rejected"}
            else None
        ),
    }


def _node_timestamps(events: tuple[AuditEvent, ...]) -> JsonObject:
    queued = next((event for event in events if event.type == "node_queued"), None)
    running = next((event for event in events if event.type == "node_running"), None)
    terminal = next(
        (
            event
            for event in reversed(events)
            if event.type
            in {
                "node_succeeded",
                "node_failed",
                "node_canceled",
                "node_skipped",
                "node_blocked",
            }
        ),
        None,
    )
    return {
        "queuedAt": queued.timestamp.isoformat() if queued else None,
        "startedAt": running.timestamp.isoformat() if running else None,
        "updatedAt": events[-1].timestamp.isoformat() if events else None,
        "completedAt": terminal.timestamp.isoformat() if terminal else None,
    }


def _run_state(
    runtime_status: WorkflowStatus,
    output: JsonObject | None,
    custom: JsonObject | None,
) -> str:
    if runtime_status == WorkflowStatus.SUSPENDED:
        return "paused"
    if runtime_status == WorkflowStatus.TERMINATED:
        return "canceled"
    for source in (output, custom):
        status = source.get("status") if source else None
        if status in {
            "pending_approval",
            "queued",
            "running",
            "paused",
            "succeeded",
            "failed",
            "canceled",
            "rejected",
        }:
            return cast(str, status)
    mapping = {
        WorkflowStatus.PENDING: "queued",
        WorkflowStatus.RUNNING: "running",
        WorkflowStatus.COMPLETED: "succeeded",
        WorkflowStatus.FAILED: "failed",
        WorkflowStatus.STALLED: "failed",
        WorkflowStatus.UNKNOWN: "failed",
    }
    return mapping[runtime_status]


def _node_state_source(output: JsonObject | None, custom: JsonObject | None) -> Mapping[str, Any]:
    for source in (output, custom):
        nodes = source.get("nodes") if source else None
        if isinstance(nodes, Mapping):
            return cast(Mapping[str, Any], nodes)
    return {}


def _intervention_reason(history: tuple[AuditEvent, ...], run_state: str) -> str | None:
    if run_state not in {"failed", "rejected", "paused", "canceled"}:
        return None
    return _latest_summary(
        history,
        {"run_failed", "run_rejected", "cap_denial", "pause", "cancel"},
    )


def _latest_summary(history: tuple[AuditEvent, ...], event_types: set[str]) -> str | None:
    event = next((item for item in reversed(history) if item.type in event_types), None)
    return event.summary if event else None


def _model_parts(reference: str | None) -> tuple[str | None, str | None]:
    provider, separator, model = (reference or "").partition(":")
    return (
        provider if separator and provider else None,
        model if separator and model else None,
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _decode_object(value: object) -> JsonObject:
    decoded = json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value
    if not isinstance(decoded, dict):
        raise ValueError("Dapr workflow payload must be a JSON object")
    return decoded


def _optional_object(value: object) -> JsonObject | None:
    if value in (None, "", b""):
        return None
    return _decode_object(value)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _not_found(run_id: str) -> JsonObject:
    return {"runId": run_id, "error": "not_found"}
