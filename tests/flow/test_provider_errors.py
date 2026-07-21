from dataclasses import asdict

import pytest

from omnigent.flow.providers import (
    AdapterRegistration,
    AdapterRequest,
    AdapterResponse,
    NodeExecutionFailure,
    NodeExecutionRequest,
    ProviderAdapterError,
    ProviderCapabilities,
    ProviderFailureRule,
    ProviderRegistry,
    ProviderRouter,
    RetryPolicy,
    TokenUsage,
    schedule_retry,
)


class FailingAdapter:
    def __init__(self, error: ProviderAdapterError) -> None:
        self.error = error
        self.calls = 0

    async def execute(self, request: AdapterRequest, *, credential: str) -> AdapterResponse:
        del request, credential
        self.calls += 1
        raise self.error


def request() -> NodeExecutionRequest:
    return NodeExecutionRequest(
        run_id="run-1",
        node_id="A",
        instructions="Run A",
        model="fake:alpha",
        default_model=None,
        allowed_tools=(),
        dependency_outputs={},
        output_schema=None,
        remaining_token_budget=100,
        attempt=2,
    )


@pytest.mark.parametrize(
    ("code", "category", "retryable"),
    [
        ("bad_credentials", "authentication", False),
        ("unsupported_model", "configuration", False),
        ("too_many_requests", "rate_limit", True),
        ("service_unavailable", "transient", True),
        ("schema_mismatch", "invalid_output", True),
        ("token_limit", "budget", False),
    ],
)
async def test_adapter_mapping_classifies_provider_failure(
    code: str,
    category: str,
    retryable: bool,
) -> None:
    adapter = FailingAdapter(ProviderAdapterError(code, "provider raw failure"))
    router = ProviderRouter(
        ProviderRegistry(
            [
                AdapterRegistration(
                    provider="fake",
                    models=frozenset({"alpha"}),
                    credential_reference="fake-key",
                    capabilities=ProviderCapabilities(True, True, True),
                    enabled=True,
                    adapter=adapter,
                    error_mapping={
                        code: ProviderFailureRule(
                            category=category,
                            retryable=retryable,
                            safe_message=f"safe {category} failure",
                        )
                    },
                )
            ]
        ),
        credentials={"fake-key": "secret"},
    )

    result = await router.execute(request())

    assert isinstance(result, NodeExecutionFailure)
    assert result.category == category
    assert result.retryable is retryable
    assert result.message == f"safe {category} failure"
    assert result.attempt == 2
    assert adapter.calls == 1


async def test_normalized_failure_preserves_safe_guidance_request_id_and_usage() -> None:
    secret = "sk-super-secret-value"
    adapter = FailingAdapter(
        ProviderAdapterError(
            "too_many_requests",
            f"raw payload with {secret}",
            request_id="request-123",
            retry_after_seconds=7,
            usage=TokenUsage(input_tokens=10, total_tokens=10),
            latency_ms=50,
        )
    )
    router = ProviderRouter(
        ProviderRegistry(
            [
                AdapterRegistration(
                    provider="fake",
                    models=frozenset({"alpha"}),
                    credential_reference="fake-key",
                    capabilities=ProviderCapabilities(True, True, True),
                    enabled=True,
                    adapter=adapter,
                    error_mapping={
                        "too_many_requests": ProviderFailureRule(
                            category="rate_limit",
                            retryable=True,
                            safe_message="provider rate limit",
                        )
                    },
                )
            ]
        ),
        credentials={"fake-key": secret},
    )

    result = await router.execute(request())
    serialized = repr(asdict(result))

    assert isinstance(result, NodeExecutionFailure)
    assert result.request_id == "request-123"
    assert result.retry_after_seconds == 7
    assert result.usage == TokenUsage(input_tokens=10, total_tokens=10)
    assert result.latency_ms == 50
    assert secret not in serialized
    assert "raw payload" not in serialized

    adapter.error = ProviderAdapterError(
        "too_many_requests",
        "raw failure",
        request_id=secret,
    )
    sensitive_id = await router.execute(request())
    assert isinstance(sensitive_id, NodeExecutionFailure)
    assert sensitive_id.request_id is None


def retryable_failure(**changes: object) -> NodeExecutionFailure:
    values = {
        "category": "rate_limit",
        "retryable": True,
        "message": "provider rate limit",
        "provider": "fake",
        "model": "alpha",
        "attempt": 2,
        "retry_after_seconds": 7.0,
    }
    values.update(changes)
    return NodeExecutionFailure(**values)


def test_retry_delay_respects_provider_guidance_and_records_next_attempt() -> None:
    decision = schedule_retry(
        retryable_failure(),
        RetryPolicy(
            max_attempts=4,
            max_elapsed_seconds=60,
            initial_delay_seconds=2,
            multiplier=2,
            max_delay_seconds=20,
        ),
        elapsed_seconds=10,
        deadline_remaining_seconds=30,
        remaining_token_budget=50,
    )

    assert decision.retry is True
    assert decision.delay_seconds == 7
    assert decision.next_attempt == 3
    assert decision.failure.attempt == 2


@pytest.mark.parametrize(
    ("failure", "policy", "context"),
    [
        (retryable_failure(retryable=False), RetryPolicy(4, 60, 2), {}),
        (retryable_failure(attempt=4), RetryPolicy(4, 60, 2), {}),
        (retryable_failure(), RetryPolicy(4, 10, 2), {"elapsed_seconds": 10}),
        (retryable_failure(), RetryPolicy(4, 60, 2), {"cancelled": True}),
        (
            retryable_failure(),
            RetryPolicy(4, 60, 2),
            {"deadline_remaining_seconds": 7},
        ),
        (retryable_failure(), RetryPolicy(4, 60, 2), {"remaining_token_budget": 0}),
    ],
)
def test_policy_limits_return_terminal_failure(
    failure: NodeExecutionFailure,
    policy: RetryPolicy,
    context: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "elapsed_seconds": 0,
        "deadline_remaining_seconds": None,
        "remaining_token_budget": None,
        "cancelled": False,
    }
    values.update(context)

    decision = schedule_retry(failure, policy, **values)

    assert decision.retry is False
    assert decision.delay_seconds is None
    assert decision.next_attempt is None
    assert decision.failure.retryable is False
