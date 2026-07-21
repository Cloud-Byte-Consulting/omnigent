"""Normalized, durable audit events for Flow run transitions."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from threading import Lock
from typing import Any, Protocol, TypeAlias, cast

from dapr.clients.exceptions import DaprInternalError
from dapr.clients.grpc._state import Concurrency, Consistency, StateOptions


class AuditEventType(StrEnum):
    VALIDATION = "validation"
    PREVIEW = "preview"
    APPROVAL = "approval"
    DENIAL = "denial"
    DISPATCH = "dispatch"
    NODE_BLOCKED = "node_blocked"
    NODE_QUEUED = "node_queued"
    NODE_RUNNING = "node_running"
    NODE_SUCCEEDED = "node_succeeded"
    NODE_FAILED = "node_failed"
    NODE_CANCELED = "node_canceled"
    NODE_SKIPPED = "node_skipped"
    RETRY = "retry"
    USAGE = "usage"
    EXPANSION = "expansion"
    CAP_DENIAL = "cap_denial"
    RUN_PENDING_APPROVAL = "run_pending_approval"
    RUN_QUEUED = "run_queued"
    RUN_RUNNING = "run_running"
    RUN_PAUSED = "run_paused"
    RUN_SUCCEEDED = "run_succeeded"
    RUN_FAILED = "run_failed"
    RUN_CANCELED = "run_canceled"
    RUN_REJECTED = "run_rejected"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"


AUDIT_EVENT_TYPES = frozenset(item.value for item in AuditEventType)
JsonObject: TypeAlias = dict[str, Any]


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One safe, provider-neutral material transition."""

    event_id: str
    run_id: str
    node_id: str | None
    type: AuditEventType
    timestamp: datetime
    source: str
    correlation_key: str
    summary: str
    metadata: JsonObject
    sequence: int = 0

    def to_dict(self) -> JsonObject:
        """Serialize with stable language-neutral field names."""
        return {
            "eventId": self.event_id,
            "runId": self.run_id,
            "nodeId": self.node_id,
            "type": self.type,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "correlationKey": self.correlation_key,
            "summary": self.summary,
            "metadata": _json_copy(self.metadata),
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AuditEvent:
        """Decode and verify a persisted event before returning it."""
        timestamp = datetime.fromisoformat(cast(str, value["timestamp"]))
        restored = create_audit_event(
            run_id=cast(str, value["runId"]),
            node_id=cast(str | None, value.get("nodeId")),
            event_type=cast(str, value["type"]),
            timestamp=timestamp,
            source=cast(str, value["source"]),
            correlation_key=cast(str, value["correlationKey"]),
            summary=cast(str, value["summary"]),
            metadata=cast(Mapping[str, Any], value["metadata"]),
        )
        if value["eventId"] != restored.event_id:
            raise ValueError("persisted audit event ID does not match its logical identity")
        sequence = value["sequence"]
        if not isinstance(sequence, int) or sequence <= 0:
            raise ValueError("persisted audit sequence must be a positive integer")
        return replace(restored, sequence=sequence)


def create_audit_event(
    *,
    run_id: str,
    node_id: str | None,
    event_type: str,
    timestamp: datetime,
    source: str,
    correlation_key: str,
    summary: str,
    metadata: Mapping[str, Any],
) -> AuditEvent:
    """Create a stable event and redact sensitive content recursively."""
    if not run_id or not source or not correlation_key or not summary:
        raise ValueError("run_id, source, correlation_key, and summary are required")
    if event_type not in AUDIT_EVENT_TYPES:
        raise ValueError(f"unsupported audit event type {event_type!r}")
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("audit timestamps must include a timezone")
    event_id = _event_id(run_id, node_id, event_type, correlation_key)
    return AuditEvent(
        event_id=event_id,
        run_id=run_id,
        node_id=node_id,
        type=AuditEventType(event_type),
        timestamp=timestamp,
        source=source,
        correlation_key=correlation_key,
        summary=_redact_string(summary),
        metadata=_json_copy(cast(JsonObject, _redact_value(dict(metadata)))),
    )


class InMemoryAuditStore:
    """Thread-safe append-only store for tests and embedded use."""

    def __init__(self) -> None:
        self._events: dict[str, list[AuditEvent]] = {}
        self._lock = Lock()

    def append(self, event: AuditEvent) -> AuditEvent:
        with self._lock:
            events = self._events.setdefault(event.run_id, [])
            duplicate = next((item for item in events if item.event_id == event.event_id), None)
            if duplicate is not None:
                return _copy_event(duplicate)
            stored = _copy_event(replace(event, sequence=len(events) + 1))
            events.append(stored)
            return _copy_event(stored)

    def history(self, run_id: str) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(
                _copy_event(event)
                for event in sorted(self._events.get(run_id, ()), key=_event_order)
            )


class StateResponse(Protocol):
    data: bytes
    etag: str


class DaprStateClient(Protocol):
    """Dapr client surface used by the durable audit store."""

    def get_state(self, store_name: str, key: str) -> StateResponse: ...

    def save_state(
        self,
        store_name: str,
        key: str,
        value: bytes | str,
        *,
        etag: str | None,
        options: StateOptions,
    ) -> object: ...


class DaprAuditStore:
    """Append-only per-run history persisted through Dapr state."""

    def __init__(
        self,
        client: DaprStateClient,
        *,
        store_name: str = "flowstatestore",
        max_attempts: int = 3,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self._client = client
        self._store_name = store_name
        self._max_attempts = max_attempts
        self._options = StateOptions(
            consistency=Consistency.strong,
            concurrency=Concurrency.first_write,
        )

    def append(self, event: AuditEvent) -> AuditEvent:
        key = _state_key(event.run_id)
        last_error: DaprInternalError | None = None
        for _ in range(self._max_attempts):
            response = self._client.get_state(self._store_name, key)
            events = list(_decode_history(response.data))
            duplicate = next((item for item in events if item.event_id == event.event_id), None)
            if duplicate is not None:
                return duplicate
            stored = replace(event, sequence=len(events) + 1)
            events.append(stored)
            value = json.dumps(
                [item.to_dict() for item in events],
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            try:
                self._client.save_state(
                    self._store_name,
                    key,
                    value,
                    etag=response.etag or None,
                    options=self._options,
                )
            except DaprInternalError as error:
                last_error = error
                continue
            return stored
        if last_error is not None:
            raise last_error
        raise RuntimeError("audit append failed without a Dapr error")

    def history(self, run_id: str) -> tuple[AuditEvent, ...]:
        response = self._client.get_state(self._store_name, _state_key(run_id))
        return tuple(sorted(_decode_history(response.data), key=_event_order))


def _event_id(run_id: str, node_id: str | None, event_type: str, correlation_key: str) -> str:
    identity = json.dumps(
        [run_id, node_id, event_type, correlation_key],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _event_order(event: AuditEvent) -> tuple[int, str]:
    return event.sequence, event.event_id


def _state_key(run_id: str) -> str:
    return f"flow-audit:{run_id}"


def _decode_history(value: bytes) -> tuple[AuditEvent, ...]:
    if not value:
        return ()
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        raise ValueError("persisted audit history must be a JSON array")
    return tuple(AuditEvent.from_dict(item) for item in decoded)


def _redact_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(
        marker in normalized
        for marker in (
            "authorization",
            "credential",
            "password",
            "secret",
            "apikey",
            "providerpayload",
            "prompt",
        )
    ) or normalized.endswith(("token",))


def _redact_string(value: str) -> str:
    patterns = (
        r"(?i)(?:authorization:\s*)?bearer\s+\S+",
        r"(?i)api[_-]?key\s*[=:]\s*\S+",
        r"\bsk-[A-Za-z0-9_-]{8,}\b",
    )
    for pattern in patterns:
        value = re.sub(pattern, "[REDACTED]", value)
    return value


def _json_copy(value: JsonObject) -> JsonObject:
    try:
        return cast(JsonObject, json.loads(json.dumps(value, allow_nan=False)))
    except (TypeError, ValueError) as error:
        raise ValueError("audit metadata must be JSON-compatible") from error


def _copy_event(event: AuditEvent) -> AuditEvent:
    return replace(event, metadata=_json_copy(event.metadata))
