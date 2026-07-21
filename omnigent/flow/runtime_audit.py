"""Replay-safe Dapr activity for durable Flow runtime audit checkpoints."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from omnigent.flow.audit import AuditEvent, create_audit_event

RUNTIME_AUDIT_ACTIVITY_NAME = "PersistFlowAuditEvents"
JsonObject = dict[str, Any]


class RuntimeAuditEventInput(BaseModel):
    """Safe normalized event draft produced by deterministic workflow code."""

    model_config = ConfigDict(extra="forbid", strict=True)

    type: str = Field(min_length=1)
    nodeId: str | None = None
    source: str = Field(min_length=1)
    correlationKey: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    metadata: JsonObject


class RuntimeAuditInput(BaseModel):
    """One atomic ordered audit checkpoint for a single run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    runId: str = Field(min_length=1)
    events: list[RuntimeAuditEventInput] = Field(min_length=1)


class AuditStore(Protocol):
    def append_many(self, events: tuple[AuditEvent, ...]) -> tuple[AuditEvent, ...]: ...


class ActivityRuntime(Protocol):
    def register_activity(
        self,
        fn: Callable[..., JsonObject],
        *,
        name: str | None = None,
    ) -> object: ...


class RuntimeAuditActivity:
    """Persist one idempotent batch without exposing workflow/provider payloads."""

    def __init__(
        self,
        store: AuditStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._clock = clock

    def execute(self, raw_input: RuntimeAuditInput | Mapping[str, Any]) -> JsonObject:
        value = (
            raw_input
            if isinstance(raw_input, RuntimeAuditInput)
            else RuntimeAuditInput.model_validate(raw_input)
        )
        timestamp = self._clock()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("runtime audit clock must return a timezone-aware timestamp")
        drafts = tuple(
            create_audit_event(
                run_id=value.runId,
                node_id=item.nodeId,
                event_type=item.type,
                timestamp=timestamp + timedelta(microseconds=index),
                source=item.source,
                correlation_key=item.correlationKey,
                summary=item.summary,
                metadata=item.metadata,
            )
            for index, item in enumerate(value.events)
        )
        stored = self._store.append_many(drafts)
        return {"eventIds": [event.event_id for event in stored]}


def register_runtime_audit_activity(
    runtime: ActivityRuntime,
    activity: RuntimeAuditActivity,
) -> Callable[[object, RuntimeAuditInput], JsonObject]:
    """Register the versioned synchronous activity used by Dapr Workflow."""

    def persist_flow_audit_events(
        _context: object,
        activity_input: RuntimeAuditInput,
    ) -> JsonObject:
        return activity.execute(activity_input)

    runtime.register_activity(persist_flow_audit_events, name=RUNTIME_AUDIT_ACTIVITY_NAME)
    return persist_flow_audit_events
