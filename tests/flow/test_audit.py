from datetime import UTC, datetime, timedelta
from math import nan

import pytest

from omnigent.flow.audit import (
    AUDIT_EVENT_TYPES,
    AuditEvent,
    InMemoryAuditStore,
    create_audit_event,
)

NOW = datetime(2026, 7, 21, tzinfo=UTC)


def event(
    event_type: str = "run_running",
    *,
    correlation_key: str = "run-1:running",
    timestamp: datetime = NOW,
    metadata: dict | None = None,
) -> AuditEvent:
    return create_audit_event(
        run_id="run-1",
        node_id=None,
        event_type=event_type,
        timestamp=timestamp,
        source="system",
        correlation_key=correlation_key,
        summary="Run entered running state",
        metadata=metadata or {},
    )


def test_append_material_transition_records_one_complete_ordered_event() -> None:
    store = InMemoryAuditStore()

    stored = store.append(event())

    assert stored.sequence == 1
    assert stored.run_id == "run-1"
    assert stored.type == "run_running"
    assert stored.timestamp == NOW
    assert stored.source == "system"
    assert stored.event_id
    assert store.history("run-1") == (stored,)
    assert stored.to_dict() == {
        "eventId": stored.event_id,
        "runId": "run-1",
        "nodeId": None,
        "type": "run_running",
        "timestamp": "2026-07-21T00:00:00+00:00",
        "source": "system",
        "correlationKey": "run-1:running",
        "summary": "Run entered running state",
        "metadata": {},
        "sequence": 1,
    }


def test_replay_with_same_logical_key_does_not_duplicate_event() -> None:
    store = InMemoryAuditStore()
    first = store.append(event())
    replay = store.append(event(timestamp=NOW + timedelta(seconds=2)))

    assert replay == first
    assert store.history("run-1") == (first,)


def test_sensitive_metadata_and_summary_are_recursively_redacted() -> None:
    credential = "sk-super-secret-value"

    stored = InMemoryAuditStore().append(
        create_audit_event(
            run_id="run-1",
            node_id="A",
            event_type="node_succeeded",
            timestamp=NOW,
            source="provider:fake",
            correlation_key="node-execution-1",
            summary=f"Authorization: Bearer {credential}",
            metadata={
                "credential": credential,
                "nested": {
                    "apiKey": credential,
                    "providerPayload": {"raw": credential},
                    "safe": "visible",
                },
                "items": [{"password": credential}, "api_key=secret-value"],
                "tokenCount": 12,
            },
        )
    )
    serialized = repr(stored.to_dict())

    assert credential not in serialized
    assert "secret-value" not in serialized
    assert stored.metadata["credential"] == "[REDACTED]"
    assert stored.metadata["nested"]["safe"] == "visible"
    assert stored.metadata["tokenCount"] == 12


@pytest.mark.parametrize("invalid", [object(), nan], ids=["object", "nan"])
def test_metadata_must_be_json_and_is_detached_after_append(invalid: object) -> None:
    with pytest.raises(ValueError, match="JSON-compatible"):
        event(metadata={"invalid": invalid})

    draft = event(metadata={"nested": {"safe": "original"}})
    store = InMemoryAuditStore()
    stored = store.append(draft)
    draft.metadata["nested"]["safe"] = "changed"
    serialized = stored.to_dict()
    serialized["metadata"]["nested"]["safe"] = "changed again"

    assert store.history("run-1")[0].metadata["nested"]["safe"] == "original"


def test_history_uses_deterministic_append_order_not_timestamps() -> None:
    store = InMemoryAuditStore()
    first = store.append(event("run_queued", correlation_key="queued", timestamp=NOW))
    second = store.append(
        event("run_running", correlation_key="running", timestamp=NOW - timedelta(seconds=1))
    )
    third = store.append(
        event("run_succeeded", correlation_key="succeeded", timestamp=NOW + timedelta(seconds=1))
    )

    assert store.history("run-1") == (first, second, third)
    assert [item.sequence for item in store.history("run-1")] == [1, 2, 3]


def test_node_execution_id_deduplicates_completion_but_attempts_remain_traceable() -> None:
    store = InMemoryAuditStore()
    completion = create_audit_event(
        run_id="run-1",
        node_id="A",
        event_type="node_succeeded",
        timestamp=NOW,
        source="system",
        correlation_key="node-execution-1",
        summary="Node completed",
        metadata={"attempt": 1},
    )
    store.append(completion)
    store.append(completion)
    store.append(
        create_audit_event(
            run_id="run-1",
            node_id="A",
            event_type="retry",
            timestamp=NOW,
            source="system",
            correlation_key="node-execution-1:attempt:2",
            summary="Retry requested",
            metadata={"attempt": 2},
        )
    )

    assert [item.type for item in store.history("run-1")] == ["node_succeeded", "retry"]


@pytest.mark.parametrize("event_type", sorted(AUDIT_EVENT_TYPES))
def test_required_event_types_are_serializable(event_type: str) -> None:
    stored = InMemoryAuditStore().append(event(event_type))
    restored = AuditEvent.from_dict(stored.to_dict())

    assert restored.type == event_type
