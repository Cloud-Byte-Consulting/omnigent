import json
from collections.abc import Iterator
from typing import Any

from omnigent.flow.activity import (
    NODE_EXECUTION_ACTIVITY_NAME,
    NodeExecutionActivity,
    register_node_execution_activity,
)
from omnigent.flow.providers import (
    AdapterRegistration,
    AdapterRequest,
    AdapterResponse,
    ProviderCapabilities,
    ProviderRegistry,
    ProviderRouter,
    RetryPolicy,
    TokenUsage,
)
from omnigent.flow.structured_output import StructuredOutputRunner
from omnigent.flow.usage import ConservativeUsagePolicy, InMemoryUsageStore, UsageService


class RecordingAdapter:
    def __init__(self, outputs: Iterator[object] | None = None) -> None:
        self.outputs = outputs or iter(({"answer": 42},))
        self.requests: list[AdapterRequest] = []

    async def execute(self, request: AdapterRequest, *, credential: str) -> AdapterResponse:
        assert credential == "secret"
        self.requests.append(request)
        return AdapterResponse(
            output=next(self.outputs),
            usage=TokenUsage(input_tokens=7, output_tokens=3, total_tokens=10),
            latency_ms=12,
            warnings=("normalized warning",),
        )


def activity(adapter: RecordingAdapter, *, max_attempts: int = 2) -> NodeExecutionActivity:
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
                )
            ]
        ),
        credentials={"fake-key": "secret"},
    )
    runner = StructuredOutputRunner(
        router,
        UsageService(
            InMemoryUsageStore(),
            missing_usage_policy=ConservativeUsagePolicy(20),
        ),
        retry_policy=RetryPolicy(max_attempts, 60, 0),
        elapsed_seconds=lambda: 0,
    )
    return NodeExecutionActivity(runner)


def activity_input(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "nodeExecutionId": "node-execution-1",
        "runId": "run-1",
        "nodeId": "C",
        "instructions": "Return an integer answer",
        "model": "fake:alpha",
        "defaultModel": None,
        "tools": ["search"],
        "dependsOn": ["A", "B"],
        "dependencyOutputs": {
            "A": {"fact": 1},
            "B": {"fact": 2},
            "unrelated": {"secret": True},
        },
        "outputSchema": {
            "type": "object",
            "properties": {"answer": {"type": "integer"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        "remainingTokenBudget": 90,
        "tokenBudget": 100,
        "attempt": 1,
    }
    value.update(changes)
    return value


async def test_activity_executes_with_allowlisted_tools_and_normalized_json() -> None:
    adapter = RecordingAdapter()

    result = await activity(adapter).execute(activity_input())

    assert result == {
        "status": "success",
        "output": {"answer": 42},
        "provider": "fake",
        "model": "alpha",
        "usage": {"inputTokens": 7, "outputTokens": 3, "totalTokens": 10},
        "latencyMs": 12,
        "attempt": 1,
        "warnings": ["normalized warning"],
    }
    assert adapter.requests[0].allowed_tools == ("search",)
    assert adapter.requests[0].node_execution_id == "node-execution-1"
    json.dumps(result)


async def test_activity_supplies_only_declared_dependency_outputs() -> None:
    adapter = RecordingAdapter()

    await activity(adapter).execute(activity_input())

    assert adapter.requests[0].dependency_outputs == {
        "A": {"fact": 1},
        "B": {"fact": 2},
    }


async def test_activity_rejects_missing_dependency_before_provider_invocation() -> None:
    adapter = RecordingAdapter()

    result = await activity(adapter).execute(
        activity_input(dependencyOutputs={"A": {"fact": 1}})
    )

    assert result["status"] == "failure"
    assert result["failure"]["category"] == "configuration"
    assert result["failure"]["retryable"] is False
    assert result["failure"]["message"] == "dependency outputs are missing: B"
    assert adapter.requests == []


async def test_activity_refuses_dispatch_when_remaining_budget_is_exhausted() -> None:
    adapter = RecordingAdapter()

    result = await activity(adapter).execute(activity_input(remainingTokenBudget=0))

    assert result["status"] == "failure"
    assert result["failure"]["category"] == "budget"
    assert result["failure"]["retryable"] is False
    assert adapter.requests == []


async def test_activity_rejects_invalid_structured_output_after_bounded_repair() -> None:
    adapter = RecordingAdapter(iter(({"answer": "wrong"}, {"answer": "still wrong"})))

    result = await activity(adapter).execute(activity_input())

    assert result["status"] == "failure"
    assert result["failure"]["category"] == "invalid_output"
    assert result["failure"]["retryable"] is False
    assert result["violations"] == [
        {
            "path": "/answer",
            "message": "value does not satisfy JSON Schema rule 'type'",
            "validator": "type",
        }
    ]
    assert len(adapter.requests) == 2


async def test_redelivery_preserves_logical_identity_and_separates_attempts() -> None:
    adapter = RecordingAdapter(iter(({"answer": 1}, {"answer": 2})))
    executor = activity(adapter)

    first = await executor.execute(activity_input(attempt=1))
    second = await executor.execute(activity_input(attempt=2))

    assert first["attempt"] == 1
    assert second["attempt"] == 2
    assert [request.node_execution_id for request in adapter.requests] == [
        "node-execution-1",
        "node-execution-1",
    ]
    assert [request.attempt for request in adapter.requests] == [1, 2]


class RecordingRuntime:
    def __init__(self) -> None:
        self.name: str | None = None
        self.handler: object | None = None

    def register_activity(self, handler: object, *, name: str | None = None) -> None:
        self.name = name
        self.handler = handler


def test_dapr_registration_boundary_returns_json_without_orchestration_side_effects() -> None:
    adapter = RecordingAdapter()
    executor = activity(adapter)
    runtime = RecordingRuntime()

    handler = register_node_execution_activity(runtime, executor)
    result = handler(object(), activity_input())

    assert runtime.name == NODE_EXECUTION_ACTIVITY_NAME
    assert runtime.handler is handler
    assert result["status"] == "success"
    assert not hasattr(executor, "schedule")
    assert not hasattr(executor, "workflow_history")
    json.dumps(result)
