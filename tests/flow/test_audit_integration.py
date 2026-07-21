from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from dapr.clients.exceptions import DaprInternalError
from dapr.clients.grpc._state import StateOptions

from omnigent.flow.audit import DaprAuditStore, create_audit_event


@dataclass
class StateResponse:
    data: bytes
    etag: str


class FakeDaprStateClient:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], tuple[bytes, int]] = {}
        self.saves: list[tuple[str, str, str | None, StateOptions]] = []

    def get_state(self, store_name: str, key: str) -> StateResponse:
        value, version = self.values.get((store_name, key), (b"", 0))
        return StateResponse(value, str(version) if version else "")

    def save_state(
        self,
        store_name: str,
        key: str,
        value: bytes | str,
        *,
        etag: str | None,
        options: StateOptions,
    ) -> None:
        current_value, current_version = self.values.get((store_name, key), (b"", 0))
        del current_value
        if etag not in (None, "", str(current_version)):
            raise AssertionError("stale etag")
        encoded = value.encode() if isinstance(value, str) else value
        self.values[(store_name, key)] = (encoded, current_version + 1)
        self.saves.append((store_name, key, etag, options))


def test_dapr_boundary_persists_deduplicated_ordered_history_across_instances() -> None:
    client = FakeDaprStateClient()
    first_store = DaprAuditStore(client)
    first = create_audit_event(
        run_id="run-1",
        node_id=None,
        event_type="run_queued",
        timestamp=datetime(2026, 7, 21, tzinfo=UTC),
        source="system",
        correlation_key="queued",
        summary="Queued",
        metadata={},
    )
    second = create_audit_event(
        run_id="run-1",
        node_id="A",
        event_type="node_running",
        timestamp=datetime(2026, 7, 21, 0, 0, 1, tzinfo=UTC),
        source="system",
        correlation_key="node-execution-1:running",
        summary="Node running",
        metadata={"attempt": 1},
    )

    first_store.append(first)
    first_store.append(second)
    restarted_store = DaprAuditStore(client)
    replay = restarted_store.append(second)
    history = restarted_store.history("run-1")

    assert replay == history[1]
    assert [item.type for item in history] == ["run_queued", "node_running"]
    assert [item.sequence for item in history] == [1, 2]
    assert len(client.saves) == 2
    assert all(save[0] == "flowstatestore" for save in client.saves)
    assert all(save[1] == "flow-audit:run-1" for save in client.saves)


def test_atomic_batch_retries_one_etag_conflict_and_replay_is_a_noop() -> None:
    class ConflictOnceClient(FakeDaprStateClient):
        def __init__(self) -> None:
            super().__init__()
            self.conflicts = 1

        def save_state(self, *args, **kwargs) -> None:
            if self.conflicts:
                self.conflicts -= 1
                raise DaprInternalError("simulated etag conflict")
            super().save_state(*args, **kwargs)

    client = ConflictOnceClient()
    store = DaprAuditStore(client)
    events = tuple(
        create_audit_event(
            run_id="run-batch",
            node_id=node_id,
            event_type=event_type,
            timestamp=datetime(2026, 7, 21, tzinfo=UTC),
            source="workflow",
            correlation_key=f"run-batch:{event_type}",
            summary=event_type,
            metadata={},
        )
        for node_id, event_type in ((None, "run_running"), ("A", "node_running"))
    )

    first = store.append_many(events)
    replay = DaprAuditStore(client).append_many(events)

    assert replay == first
    assert [event.sequence for event in first] == [1, 2]
    assert len(client.saves) == 1


def test_atomic_batch_rejects_mixed_run_ids() -> None:
    client = FakeDaprStateClient()
    events = tuple(
        create_audit_event(
            run_id=run_id,
            node_id=None,
            event_type="run_running",
            timestamp=datetime(2026, 7, 21, tzinfo=UTC),
            source="workflow",
            correlation_key=f"{run_id}:running",
            summary="running",
            metadata={},
        )
        for run_id in ("run-1", "run-2")
    )

    with pytest.raises(ValueError, match="one run_id"):
        DaprAuditStore(client).append_many(events)
