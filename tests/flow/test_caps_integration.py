from dataclasses import dataclass

from dapr.clients.grpc._state import StateOptions

from omnigent.flow.caps import CapProposal, DaprCapStore
from omnigent.flow.contracts import RunCaps


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


def test_dapr_cap_state_is_atomic_durable_and_replay_idempotent() -> None:
    client = FakeDaprStateClient()
    limits = RunCaps.model_validate(
        {"maxNodes": 2, "maxRounds": 1, "maxConcurrent": 1, "tokenBudget": 100}
    )
    proposal = CapProposal.accept_nodes("r1", ("A", "B"), round_number=1)

    first = DaprCapStore(client).apply("run-1", limits, proposal, used_tokens=0)
    replay = DaprCapStore(client).apply("run-1", limits, proposal, used_tokens=0)
    state = DaprCapStore(client).state("run-1", limits)

    assert first == replay
    assert state.accepted_node_ids == ("A", "B")
    assert len(client.saves) == 1
    assert client.saves[0][0:2] == ("flowstatestore", "flow-caps:run-1")
