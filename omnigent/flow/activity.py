"""Provider-neutral Dapr activity boundary for one Flow node."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.flow.providers import (
    AttemptAudit,
    NodeExecutionFailure,
    NodeExecutionRequest,
    NodeExecutionSuccess,
    TokenUsage,
)
from omnigent.flow.structured_output import (
    OutputViolation,
    StructuredOutputFailure,
    StructuredOutputResult,
    StructuredOutputRunner,
)

NODE_EXECUTION_ACTIVITY_NAME = "ExecuteFlowNode"
JsonObject = dict[str, Any]


def _camel_case(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class NodeActivityInput(BaseModel):
    """JSON-safe input persisted by Dapr for one logical node execution."""

    model_config = ConfigDict(
        alias_generator=_camel_case,
        extra="forbid",
        populate_by_name=True,
        strict=True,
    )

    node_execution_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    model: str | None
    default_model: str | None
    tools: list[str]
    depends_on: list[str]
    dependency_outputs: dict[str, Any]
    output_schema: dict[str, Any] | None
    remaining_token_budget: int = Field(ge=0)
    token_budget: int = Field(gt=0)
    attempt: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_collections(self) -> NodeActivityInput:
        if any(not item for item in (*self.tools, *self.depends_on)):
            raise ValueError("tools and dependency IDs must be non-empty")
        if len(self.tools) != len(set(self.tools)):
            raise ValueError("tools must be unique")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("dependency IDs must be unique")
        if self.remaining_token_budget > self.token_budget:
            raise ValueError("remaining token budget cannot exceed the run token budget")
        return self


class ActivityRuntime(Protocol):
    """Subset of the Dapr runtime used to register this activity."""

    def register_activity(
        self,
        fn: Callable[..., JsonObject],
        *,
        name: str | None = None,
    ) -> object: ...


class NodeExecutionActivity:
    """Execute exactly one node without workflow scheduling side effects."""

    def __init__(self, runner: StructuredOutputRunner) -> None:
        self._runner = runner

    async def execute(
        self,
        raw_input: NodeActivityInput | Mapping[str, Any],
    ) -> JsonObject:
        """Validate, filter, execute, and serialize one activity delivery."""
        activity_input = (
            raw_input
            if isinstance(raw_input, NodeActivityInput)
            else NodeActivityInput.model_validate(raw_input)
        )
        if activity_input.remaining_token_budget == 0:
            return _serialize_result(
                NodeExecutionFailure(
                    category="budget",
                    retryable=False,
                    message="token budget is exhausted",
                    provider=_provider(activity_input.model or activity_input.default_model),
                    model=_model(activity_input.model or activity_input.default_model),
                    attempt=activity_input.attempt,
                )
            )
        missing = sorted(set(activity_input.depends_on) - activity_input.dependency_outputs.keys())
        if missing:
            return _serialize_result(
                NodeExecutionFailure(
                    category="configuration",
                    retryable=False,
                    message=f"dependency outputs are missing: {', '.join(missing)}",
                    provider=_provider(activity_input.model or activity_input.default_model),
                    model=_model(activity_input.model or activity_input.default_model),
                    attempt=activity_input.attempt,
                )
            )

        dependencies = {
            dependency: deepcopy(activity_input.dependency_outputs[dependency])
            for dependency in activity_input.depends_on
        }
        result = await self._runner.execute(
            NodeExecutionRequest(
                run_id=activity_input.run_id,
                node_id=activity_input.node_id,
                instructions=activity_input.instructions,
                model=activity_input.model,
                default_model=activity_input.default_model,
                allowed_tools=tuple(activity_input.tools),
                dependency_outputs=dependencies,
                output_schema=deepcopy(activity_input.output_schema),
                remaining_token_budget=activity_input.remaining_token_budget,
                attempt=activity_input.attempt,
                node_execution_id=activity_input.node_execution_id,
            ),
            token_budget=activity_input.token_budget,
        )
        return _serialize_result(result)


def register_node_execution_activity(
    runtime: ActivityRuntime,
    activity: NodeExecutionActivity,
) -> Callable[[object, NodeActivityInput], JsonObject]:
    """Register the synchronous adapter required by Dapr Workflow 1.18.0."""

    def execute_flow_node(
        _context: object,
        activity_input: NodeActivityInput,
    ) -> JsonObject:
        value = (
            activity_input
            if isinstance(activity_input, NodeActivityInput)
            else NodeActivityInput.model_validate(activity_input)
        )
        return asyncio.run(activity.execute(value))

    runtime.register_activity(execute_flow_node, name=NODE_EXECUTION_ACTIVITY_NAME)
    return execute_flow_node


def _serialize_result(result: StructuredOutputResult) -> JsonObject:
    if isinstance(result, NodeExecutionSuccess):
        return {
            "status": "success",
            "output": deepcopy(result.output),
            "provider": result.provider,
            "model": result.model,
            "usage": _serialize_usage(result.usage),
            "latencyMs": result.latency_ms,
            "attempt": result.attempt,
            "warnings": list(result.warnings),
            "attemptHistory": _serialize_attempt_history(result.attempt_history),
        }

    structured = isinstance(result, StructuredOutputFailure)
    violations: tuple[OutputViolation, ...] = ()
    if structured:
        assert isinstance(result, StructuredOutputFailure)
        failure = result.failure
        violations = result.violations
    else:
        failure = cast(NodeExecutionFailure, result)
    serialized: JsonObject = {
        "status": "failure",
        "failure": {
            "category": failure.category,
            "retryable": failure.retryable,
            "message": failure.message,
            "provider": failure.provider,
            "model": failure.model,
            "attempt": failure.attempt,
            "requestId": failure.request_id,
            "retryAfterSeconds": failure.retry_after_seconds,
            "usage": _serialize_usage(failure.usage),
            "latencyMs": failure.latency_ms,
            "providerInvoked": failure.provider_invoked,
        },
        "attemptHistory": _serialize_attempt_history(failure.attempt_history),
    }
    if structured:
        serialized["violations"] = [
            {
                "path": violation.path,
                "message": violation.message,
                "validator": violation.validator,
            }
            for violation in violations
        ]
    return serialized


def _serialize_usage(usage: TokenUsage) -> JsonObject:
    return {
        "inputTokens": usage.input_tokens,
        "outputTokens": usage.output_tokens,
        "totalTokens": usage.total_tokens,
    }


def _serialize_attempt_history(history: tuple[AttemptAudit, ...]) -> list[JsonObject]:
    return [
        {
            "attempt": item.attempt,
            "provider": item.provider,
            "model": item.model,
            "succeeded": item.succeeded,
            "category": item.category,
            "estimated": item.estimated,
            "usage": _serialize_usage(item.usage),
        }
        for item in history
    ]


def _provider(reference: str | None) -> str | None:
    provider, separator, _ = (reference or "").partition(":")
    return provider if separator and provider else None


def _model(reference: str | None) -> str | None:
    _, separator, model = (reference or "").partition(":")
    return model if separator and model else None
