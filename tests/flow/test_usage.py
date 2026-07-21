from omnigent.flow.providers import TokenUsage
from omnigent.flow.usage import (
    ConservativeUsagePolicy,
    InMemoryUsageStore,
    UsageService,
)


def service() -> UsageService:
    return UsageService(
        InMemoryUsageStore(),
        missing_usage_policy=ConservativeUsagePolicy(tokens_per_attempt=40),
    )


def test_aggregates_successful_node_usage_and_remaining_budget() -> None:
    usage = service()

    usage.record_attempt(
        run_id="run-1",
        idempotency_key="A:attempt:1",
        node_id="A",
        attempt=1,
        provider="fake",
        model="alpha",
        succeeded=True,
        usage=TokenUsage(total_tokens=100),
        extra_tokens={"cachedInput": 20},
        token_budget=300,
    )
    state = usage.record_attempt(
        run_id="run-1",
        idempotency_key="B:attempt:1",
        node_id="B",
        attempt=1,
        provider="fake",
        model="alpha",
        succeeded=True,
        usage=TokenUsage(input_tokens=100, output_tokens=50),
        token_budget=300,
    )

    assert state.used_tokens == 250
    assert state.remaining_tokens == 50
    assert state.limit_tokens == 300
    assert state.records[0].extra_tokens == {"cachedInput": 20}


def test_charged_failed_attempt_counts_exactly_once_during_replay() -> None:
    usage = service()
    arguments = {
        "run_id": "run-1",
        "idempotency_key": "A:attempt:1",
        "node_id": "A",
        "attempt": 1,
        "provider": "fake",
        "model": "alpha",
        "succeeded": False,
        "usage": TokenUsage(total_tokens=25),
        "token_budget": 100,
    }

    first = usage.record_attempt(**arguments)
    replay = usage.record_attempt(**arguments)

    assert first == replay
    assert replay.used_tokens == 25
    assert len(replay.records) == 1


def test_prevents_dispatch_when_request_cannot_fit() -> None:
    usage = service()
    usage.record_attempt(
        run_id="run-1",
        idempotency_key="A:attempt:1",
        node_id="A",
        attempt=1,
        provider="fake",
        model="alpha",
        succeeded=True,
        usage=TokenUsage(total_tokens=80),
        token_budget=100,
    )

    decision = usage.check_dispatch("run-1", token_budget=100, required_tokens=30)

    assert decision.allowed is False
    assert decision.failure is not None
    assert decision.failure.code == "budget_exceeded"
    assert decision.failure.current == 80
    assert decision.failure.remaining == 20
    assert decision.failure.limit == 100
    assert decision.failure.retryable is False


def test_missing_usage_applies_conservative_policy_and_warning() -> None:
    usage = service()

    state = usage.record_attempt(
        run_id="run-1",
        idempotency_key="A:attempt:1",
        node_id="A",
        attempt=1,
        provider="opaque",
        model="unknown-usage",
        succeeded=True,
        usage=None,
        token_budget=100,
    )

    assert state.used_tokens == 40
    assert state.records[0].estimated is True
    assert state.records[0].warnings == ("provider usage unavailable; counted 40 tokens",)
    assert state.warnings == state.records[0].warnings


def test_actual_overage_is_persisted_and_stops_further_dispatch() -> None:
    usage = service()

    state = usage.record_attempt(
        run_id="run-1",
        idempotency_key="A:attempt:1",
        node_id="A",
        attempt=1,
        provider="fake",
        model="alpha",
        succeeded=True,
        usage=TokenUsage(total_tokens=120),
        token_budget=100,
    )
    decision = usage.check_dispatch("run-1", token_budget=100, required_tokens=1)

    assert state.used_tokens == 120
    assert state.remaining_tokens == 0
    assert state.cap_reached is True
    assert decision.allowed is False
    assert decision.failure is not None
    assert decision.failure.current == 120


def test_persisted_usage_is_detached_from_returned_metadata() -> None:
    usage = service()
    arguments = {
        "run_id": "run-1",
        "idempotency_key": "A:attempt:1",
        "node_id": "A",
        "attempt": 1,
        "provider": "fake",
        "model": "alpha",
        "succeeded": True,
        "usage": TokenUsage(total_tokens=10),
        "extra_tokens": {"cachedInput": 5},
        "token_budget": 100,
    }
    returned = usage.record_attempt(**arguments)
    returned.records[0].extra_tokens["cachedInput"] = 99

    replay = usage.record_attempt(**arguments)

    assert replay.records[0].extra_tokens == {"cachedInput": 5}
