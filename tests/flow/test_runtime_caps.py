from datetime import UTC, datetime

from omnigent.flow.audit import InMemoryAuditStore
from omnigent.flow.caps import InMemoryCapStore
from omnigent.flow.runtime_caps import (
    RUNTIME_CAP_ACTIVITY_NAME,
    RuntimeCapActivity,
    register_runtime_cap_activity,
)
from omnigent.flow.usage import ConservativeUsagePolicy, InMemoryUsageStore, UsageService


def activity() -> tuple[
    RuntimeCapActivity,
    InMemoryCapStore,
    InMemoryAuditStore,
    UsageService,
]:
    store = InMemoryCapStore()
    audit = InMemoryAuditStore()
    usage = UsageService(
        InMemoryUsageStore(),
        missing_usage_policy=ConservativeUsagePolicy(20),
    )
    return (
        RuntimeCapActivity(
            store,
            usage=usage,
            audit=audit,
            clock=lambda: datetime(2026, 7, 21, tzinfo=UTC),
        ),
        store,
        audit,
        usage,
    )


def limits(**changes: int) -> dict[str, int]:
    value = {
        "maxNodes": 3,
        "maxRounds": 2,
        "maxConcurrent": 1,
        "tokenBudget": 100,
    }
    value.update(changes)
    return value


class RecordingRuntime:
    def __init__(self) -> None:
        self.name: str | None = None
        self.handler = None

    def register_activity(self, handler, *, name=None):
        self.name = name
        self.handler = handler


def test_runtime_cap_activity_tracks_queue_reservation_completion_and_replay() -> None:
    executor, _store, _audit, _usage = activity()
    accepted = executor.execute(
        {
            "runId": "run-1",
            "limits": limits(),
            "kind": "accept_nodes",
            "idempotencyKey": "run-1:round:1:accept",
            "nodeIds": ["A", "B"],
            "roundNumber": 1,
        }
    )
    dispatched = executor.execute(
        {
            "runId": "run-1",
            "limits": limits(),
            "kind": "dispatch",
            "idempotencyKey": "run-1:A:dispatch",
            "nodeId": "A",
            "requiredTokens": 10,
        }
    )
    replay = executor.execute(
        {
            "runId": "run-1",
            "limits": limits(),
            "kind": "dispatch",
            "idempotencyKey": "run-1:A:dispatch",
            "nodeId": "A",
            "requiredTokens": 10,
        }
    )
    queued = executor.execute(
        {
            "runId": "run-1",
            "limits": limits(),
            "kind": "dispatch",
            "idempotencyKey": "run-1:B:dispatch",
            "nodeId": "B",
            "requiredTokens": 10,
        }
    )
    completed = executor.execute(
        {
            "runId": "run-1",
            "limits": limits(),
            "kind": "complete",
            "idempotencyKey": "run-1:A:complete",
            "nodeId": "A",
        }
    )

    assert accepted["state"]["acceptedNodeIds"] == ["A", "B"]
    assert accepted["state"]["currentRound"] == 1
    assert accepted["state"]["queuedNodeIds"] == ["A", "B"]
    assert dispatched["state"]["runningNodeIds"] == ["A"]
    assert dispatched["state"]["queuedNodeIds"] == ["B"]
    assert dispatched["state"]["reservedTokens"] == {"A": 10}
    assert replay == dispatched
    assert queued["decision"]["queued"] is True
    assert completed["state"]["runningNodeIds"] == []
    assert completed["state"]["queuedNodeIds"] == ["B"]
    assert completed["state"]["reservedTokens"] == {}


def test_runtime_cap_activity_denies_before_dispatch_with_exact_audit_values() -> None:
    executor, _store, audit, usage = activity()
    executor.execute(
        {
            "runId": "run-1",
            "limits": limits(),
            "kind": "accept_nodes",
            "idempotencyKey": "accept",
            "nodeIds": ["A"],
            "roundNumber": 1,
        }
    )
    usage.record_attempt(
        run_id="run-1",
        idempotency_key="used",
        node_id="prior",
        attempt=1,
        provider="fake",
        model="alpha",
        succeeded=True,
        usage=None,
        token_budget=100,
    )

    denied = executor.execute(
        {
            "runId": "run-1",
            "limits": limits(),
            "kind": "dispatch",
            "idempotencyKey": "dispatch-A",
            "nodeId": "A",
            "requiredTokens": 81,
        }
    )

    assert denied["decision"] == {
        "allowed": False,
        "queued": False,
        "cap": "tokenBudget",
        "current": 20,
        "proposed": 101,
        "limit": 100,
        "message": "proposal exceeds tokenBudget",
    }
    assert denied["state"]["runningNodeIds"] == []
    assert audit.history("run-1")[-1].metadata == {
        "cap": "tokenBudget",
        "current": 20,
        "proposed": 101,
        "limit": 100,
    }


def test_runtime_cap_activity_accepts_only_new_nodes_in_the_next_round() -> None:
    executor, _store, _audit, _usage = activity()
    first = executor.execute(
        {
            "runId": "run-1",
            "limits": limits(),
            "kind": "accept_nodes",
            "idempotencyKey": "round-1",
            "nodeIds": ["A"],
            "roundNumber": 1,
        }
    )
    expanded = executor.execute(
        {
            "runId": "run-1",
            "limits": limits(),
            "kind": "accept_nodes",
            "idempotencyKey": "round-2",
            "nodeIds": ["A", "B"],
            "roundNumber": 2,
        }
    )

    assert first["state"]["acceptedNodeIds"] == ["A"]
    assert expanded["state"]["acceptedNodeIds"] == ["A", "B"]
    assert expanded["state"]["currentRound"] == 2
    assert expanded["state"]["queuedNodeIds"] == ["A", "B"]


def test_runtime_cap_activity_registers_the_versioned_dapr_name() -> None:
    executor, _store, _audit, _usage = activity()
    runtime = RecordingRuntime()

    handler = register_runtime_cap_activity(runtime, executor)
    result = handler(
        None,
        {
            "runId": "run-1",
            "limits": limits(),
            "kind": "accept_nodes",
            "idempotencyKey": "accept",
            "nodeIds": ["A"],
            "roundNumber": 1,
        },
    )

    assert runtime.name == RUNTIME_CAP_ACTIVITY_NAME == "ApplyFlowCapTransition"
    assert runtime.handler is handler
    assert result["decision"]["allowed"] is True
