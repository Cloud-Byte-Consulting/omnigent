from dataclasses import dataclass
from datetime import UTC, datetime

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
