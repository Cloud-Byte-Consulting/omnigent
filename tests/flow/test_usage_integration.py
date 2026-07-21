from dataclasses import dataclass

from dapr.clients.grpc._state import StateOptions

from omnigent.flow.providers import TokenUsage
from omnigent.flow.usage import (
    ConservativeUsagePolicy,
    DaprUsageStore,
    UsageService,
)


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
        _, version = self.values.get((store_name, key), (b"", 0))
        if etag not in (None, "", str(version)):
            raise AssertionError("stale etag")
        encoded = value.encode() if isinstance(value, str) else value
        self.values[(store_name, key)] = (encoded, version + 1)
        self.saves.append((store_name, key, etag, options))


def test_dapr_boundary_persists_usage_before_next_dispatch_and_deduplicates_replay() -> None:
    client = FakeDaprStateClient()
    first = UsageService(
        DaprUsageStore(client),
        missing_usage_policy=ConservativeUsagePolicy(40),
    )
    arguments = {
        "run_id": "run-1",
        "idempotency_key": "A:attempt:1",
        "node_id": "A",
        "attempt": 1,
        "provider": "fake",
        "model": "alpha",
        "succeeded": False,
        "usage": TokenUsage(total_tokens=75),
        "token_budget": 100,
    }
    persisted = first.record_attempt(**arguments)

    restarted = UsageService(
        DaprUsageStore(client),
        missing_usage_policy=ConservativeUsagePolicy(40),
    )
    replay = restarted.record_attempt(**arguments)
    decision = restarted.check_dispatch("run-1", token_budget=100, required_tokens=30)

    assert persisted == replay
    assert replay.used_tokens == 75
    assert len(replay.records) == 1
    assert decision.allowed is False
    assert len(client.saves) == 1
    assert client.saves[0][0:2] == ("flowstatestore", "flow-usage:run-1")
