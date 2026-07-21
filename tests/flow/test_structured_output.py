from collections.abc import Iterator

from omnigent.flow.providers import (
    AdapterRegistration,
    AdapterRequest,
    AdapterResponse,
    NodeExecutionFailure,
    NodeExecutionRequest,
    NodeExecutionSuccess,
    ProviderCapabilities,
    ProviderRegistry,
    ProviderRouter,
    RetryPolicy,
    TokenUsage,
)
from omnigent.flow.structured_output import (
    StructuredOutputFailure,
    StructuredOutputRunner,
    validate_output,
)
from omnigent.flow.usage import ConservativeUsagePolicy, InMemoryUsageStore, UsageService

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
    "additionalProperties": False,
}


class SequenceAdapter:
    def __init__(self, outputs: Iterator[object]) -> None:
        self.outputs = outputs
        self.calls: list[AdapterRequest] = []

    async def execute(self, request: AdapterRequest, *, credential: str) -> AdapterResponse:
        del credential
        self.calls.append(request)
        return AdapterResponse(
            output=next(self.outputs),
            usage=TokenUsage(total_tokens=10),
        )


def request(*, output_schema: dict | None = SCHEMA) -> NodeExecutionRequest:
    return NodeExecutionRequest(
        run_id="run-1",
        node_id="A",
        instructions="Return an answer",
        model="fake:alpha",
        default_model=None,
        allowed_tools=(),
        dependency_outputs={},
        output_schema=output_schema,
        remaining_token_budget=100,
        attempt=1,
    )


def runner(
    adapter: SequenceAdapter,
    *,
    structured_output: bool = True,
    store: InMemoryUsageStore | None = None,
) -> StructuredOutputRunner:
    router = ProviderRouter(
        ProviderRegistry(
            [
                AdapterRegistration(
                    provider="fake",
                    models=frozenset({"alpha"}),
                    credential_reference="fake-key",
                    capabilities=ProviderCapabilities(True, structured_output, True),
                    enabled=True,
                    adapter=adapter,
                )
            ]
        ),
        credentials={"fake-key": "secret"},
    )
    usage = UsageService(
        store or InMemoryUsageStore(),
        missing_usage_policy=ConservativeUsagePolicy(20),
    )
    return StructuredOutputRunner(
        router,
        usage,
        retry_policy=RetryPolicy(2, 60, 0),
        elapsed_seconds=lambda: 0,
    )


async def test_accepts_locally_conforming_output_for_dependents() -> None:
    adapter = SequenceAdapter(iter(({"answer": 42},)))

    result = await runner(adapter).execute(request(), token_budget=100)

    assert isinstance(result, NodeExecutionSuccess)
    assert result.output == {"answer": 42}
    assert len(adapter.calls) == 1


async def test_rejects_nonconforming_output_with_stable_json_paths() -> None:
    adapter = SequenceAdapter(iter(({"answer": "wrong", "extra": True},)))

    result = await runner(adapter).execute(
        request(),
        token_budget=100,
        repair_enabled=False,
    )

    assert isinstance(result, StructuredOutputFailure)
    assert result.failure.category == "invalid_output"
    assert result.failure.retryable is False
    assert [error.path for error in result.violations] == ["/", "/answer"]
    assert result.output is None


async def test_repair_reuses_schema_and_errors_and_records_both_attempts() -> None:
    adapter = SequenceAdapter(iter(({"answer": "wrong"}, {"answer": 42})))
    store = InMemoryUsageStore()
    executor = runner(adapter, store=store)

    result = await executor.execute(request(), token_budget=100)
    state = store.state("run-1", token_budget=100)

    assert isinstance(result, NodeExecutionSuccess)
    assert result.output == {"answer": 42}
    assert [record.attempt for record in state.records] == [1, 2]
    assert [record.succeeded for record in state.records] == [False, True]
    assert state.used_tokens == 20
    assert adapter.calls[1].output_schema == SCHEMA
    assert adapter.calls[1].repair_errors
    assert adapter.calls[1].attempt == 2


async def test_repair_errors_do_not_echo_invalid_provider_values() -> None:
    secret = "sk-super-secret-value"
    adapter = SequenceAdapter(iter(({"answer": secret}, {"answer": 42})))

    result = await runner(adapter).execute(request(), token_budget=100)

    assert isinstance(result, NodeExecutionSuccess)
    assert secret not in repr(adapter.calls[1].repair_errors)


async def test_incapable_adapter_is_rejected_without_provider_call() -> None:
    adapter = SequenceAdapter(iter(({"answer": 42},)))

    result = await runner(adapter, structured_output=False).execute(
        request(),
        token_budget=100,
    )

    assert isinstance(result, NodeExecutionFailure)
    assert result.category == "configuration"
    assert result.retryable is False
    assert adapter.calls == []


async def test_provider_strict_claim_never_skips_local_validation() -> None:
    adapter = SequenceAdapter(iter(({"answer": "still wrong"},)))

    result = await runner(adapter).execute(
        request(),
        token_budget=100,
        repair_enabled=False,
    )

    assert isinstance(result, StructuredOutputFailure)
    assert result.violations[0].path == "/answer"


async def test_node_without_schema_returns_unconstrained_normalized_output() -> None:
    adapter = SequenceAdapter(iter(("plain text",)))

    result = await runner(adapter).execute(request(output_schema=None), token_budget=100)

    assert isinstance(result, NodeExecutionSuccess)
    assert result.output == "plain text"


def test_validation_errors_escape_json_pointer_paths() -> None:
    errors = validate_output(
        {"a/b": "wrong"},
        {
            "type": "object",
            "properties": {"a/b": {"type": "integer"}},
        },
    )

    assert errors[0].path == "/a~1b"
