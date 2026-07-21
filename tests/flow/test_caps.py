from datetime import UTC, datetime

import pytest

from omnigent.flow.audit import InMemoryAuditStore
from omnigent.flow.caps import CapProposal, CapService, CapState, InMemoryCapStore, _apply
from omnigent.flow.contracts import RunCaps
from omnigent.flow.providers import TokenUsage
from omnigent.flow.usage import ConservativeUsagePolicy, InMemoryUsageStore, UsageService


def caps(**changes: int) -> RunCaps:
    values = {
        "maxNodes": 5,
        "maxRounds": 2,
        "maxConcurrent": 2,
        "tokenBudget": 100,
    }
    values.update(changes)
    return RunCaps.model_validate(values)


def service() -> tuple[CapService, InMemoryCapStore, InMemoryAuditStore, UsageService]:
    store = InMemoryCapStore()
    audit = InMemoryAuditStore()
    usage = UsageService(
        InMemoryUsageStore(),
        missing_usage_policy=ConservativeUsagePolicy(20),
    )
    return (
        CapService(
            store,
            usage=usage,
            audit=audit,
            clock=lambda: datetime(2026, 7, 21, tzinfo=UTC),
        ),
        store,
        audit,
        usage,
    )


@pytest.mark.parametrize(
    ("cap_name", "prepare", "act", "expected"),
    [
        (
            "maxNodes",
            lambda api, limits, usage: api.accept_nodes(
                "run-1", limits, node_ids=("A", "B"), round_number=1, idempotency_key="r1"
            ),
            lambda api, limits: api.accept_nodes(
                "run-1",
                limits,
                node_ids=("C", "D"),
                round_number=2,
                idempotency_key="r2",
            ),
            (2, 4, 3),
        ),
        (
            "maxRounds",
            lambda api, limits, usage: (
                api.accept_nodes(
                    "run-1", limits, node_ids=("A",), round_number=1, idempotency_key="r1"
                ),
                api.accept_nodes(
                    "run-1", limits, node_ids=("B",), round_number=2, idempotency_key="r2"
                ),
            ),
            lambda api, limits: api.accept_nodes(
                "run-1",
                limits,
                node_ids=("C",),
                round_number=3,
                idempotency_key="r3",
            ),
            (2, 3, 2),
        ),
        (
            "tokenBudget",
            lambda api, limits, usage: (
                api.accept_nodes(
                    "run-1", limits, node_ids=("B",), round_number=1, idempotency_key="r1"
                ),
                usage.record_attempt(
                    run_id="run-1",
                    idempotency_key="usage-1",
                    node_id="A",
                    attempt=1,
                    provider="fake",
                    model="alpha",
                    succeeded=True,
                    usage=TokenUsage(total_tokens=90),
                    token_budget=limits.token_budget,
                ),
            ),
            lambda api, limits: api.request_dispatch(
                "run-1",
                limits,
                node_id="B",
                required_tokens=11,
                idempotency_key="dispatch-B",
            ),
            (90, 101, 100),
        ),
    ],
)
def test_hard_cap_denials_return_values_and_are_audited(
    cap_name: str,
    prepare: object,
    act: object,
    expected: tuple[int, int, int],
) -> None:
    api, _store, audit, usage = service()
    limits = caps(maxNodes=3)
    prepare(api, limits, usage)  # type: ignore[operator]

    decision = act(api, limits)  # type: ignore[operator]

    assert decision.allowed is False
    assert decision.queued is False
    assert decision.cap == cap_name
    assert (decision.current, decision.proposed, decision.limit) == expected
    event = audit.history("run-1")[-1]
    assert event.type == "cap_denial"
    assert event.metadata == {
        "cap": cap_name,
        "current": expected[0],
        "proposed": expected[1],
        "limit": expected[2],
    }


def test_exact_hard_limits_are_allowed() -> None:
    api, _store, _audit, usage = service()
    limits = caps(maxNodes=2, maxRounds=1)

    nodes = api.accept_nodes(
        "run-1", limits, node_ids=("A", "B"), round_number=1, idempotency_key="r1"
    )
    usage.record_attempt(
        run_id="run-1",
        idempotency_key="usage-1",
        node_id="old",
        attempt=1,
        provider="fake",
        model="alpha",
        succeeded=True,
        usage=TokenUsage(total_tokens=90),
        token_budget=100,
    )
    tokens = api.request_dispatch(
        "run-1", limits, node_id="A", required_tokens=10, idempotency_key="dispatch-A"
    )

    assert nodes.allowed is True
    assert tokens.allowed is True
    assert (tokens.proposed, tokens.limit) == (100, 100)


def test_concurrency_excess_is_queued_in_stable_order() -> None:
    api, store, _audit, _usage = service()
    limits = caps()
    api.accept_nodes(
        "run-1",
        limits,
        node_ids=("A", "B", "C", "D", "E"),
        round_number=1,
        idempotency_key="r1",
    )

    decisions = [
        api.request_dispatch(
            "run-1",
            limits,
            node_id=node_id,
            required_tokens=1,
            idempotency_key=f"dispatch-{node_id}",
        )
        for node_id in ("A", "B", "C", "D", "E")
    ]
    state = store.state("run-1", limits)

    assert [decision.allowed for decision in decisions] == [True, True, False, False, False]
    assert [decision.queued for decision in decisions] == [False, False, True, True, True]
    assert state.running_node_ids == ("A", "B")
    assert state.queued_node_ids == ("C", "D", "E")


def test_replay_does_not_increment_counts_or_reservations() -> None:
    api, store, _audit, _usage = service()
    limits = caps()

    first_nodes = api.accept_nodes(
        "run-1", limits, node_ids=("A",), round_number=1, idempotency_key="r1"
    )
    replay_nodes = api.accept_nodes(
        "run-1", limits, node_ids=("A",), round_number=1, idempotency_key="r1"
    )
    first_dispatch = api.request_dispatch(
        "run-1", limits, node_id="A", required_tokens=20, idempotency_key="dispatch-A"
    )
    replay_dispatch = api.request_dispatch(
        "run-1", limits, node_id="A", required_tokens=20, idempotency_key="dispatch-A"
    )
    state = store.state("run-1", limits)

    assert first_nodes == replay_nodes
    assert first_dispatch == replay_dispatch
    assert state.accepted_node_ids == ("A",)
    assert state.running_node_ids == ("A",)
    assert state.reserved_tokens == {"A": 20}


def test_new_idempotency_key_cannot_spend_a_round_on_only_existing_nodes() -> None:
    api, store, _audit, _usage = service()
    limits = caps()
    api.accept_nodes("run-1", limits, node_ids=("A",), round_number=1, idempotency_key="r1")

    with pytest.raises(ValueError, match="at least one new node"):
        api.accept_nodes(
            "run-1", limits, node_ids=("A",), round_number=2, idempotency_key="different-key"
        )

    assert store.state("run-1", limits).current_round == 1


def test_idempotency_key_cannot_be_reused_for_a_different_proposal() -> None:
    api, _store, _audit, _usage = service()
    limits = caps()
    api.accept_nodes("run-1", limits, node_ids=("A",), round_number=1, idempotency_key="accept")

    with pytest.raises(ValueError, match="reused for a different cap proposal"):
        api.accept_nodes(
            "run-1", limits, node_ids=("B",), round_number=1, idempotency_key="accept"
        )


def test_legacy_decision_without_fingerprint_remains_replayable() -> None:
    limits = caps()
    state = CapState.from_dict(
        {
            "runId": "run-1",
            "limits": limits.model_dump(mode="json", by_alias=True),
            "acceptedNodeIds": ["A"],
            "currentRound": 1,
            "runningNodeIds": [],
            "queuedNodeIds": ["A"],
            "reservedTokens": {},
            "decisions": [
                {
                    "idempotencyKey": "accept",
                    "decision": {
                        "allowed": True,
                        "queued": False,
                        "cap": None,
                        "current": 0,
                        "proposed": 1,
                        "limit": 5,
                        "message": "nodes accepted within run caps",
                    },
                }
            ],
        }
    )

    replayed, decision, changed = _apply(
        state,
        limits,
        CapProposal.accept_nodes("accept", ("A",), round_number=1),
        used_tokens=0,
    )

    assert replayed == state
    assert decision.allowed is True
    assert changed is False


def test_completion_releases_reservation_without_implicitly_dispatching_queue() -> None:
    api, store, _audit, _usage = service()
    limits = caps(maxConcurrent=1)
    api.accept_nodes("run-1", limits, node_ids=("A", "B"), round_number=1, idempotency_key="r1")
    api.request_dispatch(
        "run-1", limits, node_id="A", required_tokens=20, idempotency_key="dispatch-A"
    )
    api.request_dispatch(
        "run-1", limits, node_id="B", required_tokens=20, idempotency_key="dispatch-B"
    )

    api.complete_dispatch("run-1", limits, node_id="A", idempotency_key="complete-A")
    state = store.state("run-1", limits)

    assert state.running_node_ids == ()
    assert state.queued_node_ids == ("B",)
    assert state.reserved_tokens == {}
