"""Local JSON Schema enforcement and bounded repair for Flow nodes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, TypeAlias

from jsonschema import Draft202012Validator

from omnigent.flow.providers import (
    NodeExecutionFailure,
    NodeExecutionRequest,
    NodeExecutionSuccess,
    ProviderRouter,
    RetryPolicy,
    TokenUsage,
    schedule_retry,
)
from omnigent.flow.usage import BudgetFailure, RunUsageState, UsageService

JsonObject: TypeAlias = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class OutputViolation:
    """One deterministic local JSON Schema validation failure."""

    path: str
    message: str
    validator: str


@dataclass(frozen=True, slots=True)
class StructuredOutputFailure:
    """Invalid provider output that must never reach dependents."""

    failure: NodeExecutionFailure
    violations: tuple[OutputViolation, ...]
    output: None = None


StructuredOutputResult: TypeAlias = (
    NodeExecutionSuccess | NodeExecutionFailure | StructuredOutputFailure
)


def validate_output(output: Any, schema: JsonObject) -> tuple[OutputViolation, ...]:
    """Validate provider output locally with stable JSON-pointer paths."""
    validator = Draft202012Validator(schema)
    violations = (
        OutputViolation(
            path=_json_pointer(error.absolute_path),
            message=f"value does not satisfy JSON Schema rule {error.validator!r}",
            validator=str(error.validator),
        )
        for error in validator.iter_errors(output)
    )
    return tuple(sorted(violations, key=lambda item: (item.path, item.validator, item.message)))


class StructuredOutputRunner:
    """Execute, locally validate, and optionally repair one node output."""

    def __init__(
        self,
        router: ProviderRouter,
        usage: UsageService,
        *,
        retry_policy: RetryPolicy,
        elapsed_seconds: Callable[[], float],
    ) -> None:
        self._router = router
        self._usage = usage
        self._retry_policy = retry_policy
        self._elapsed_seconds = elapsed_seconds

    async def execute(
        self,
        request: NodeExecutionRequest,
        *,
        token_budget: int,
        repair_enabled: bool = True,
        cancelled: bool = False,
        deadline_remaining_seconds: float | None = None,
        minimum_call_tokens: int = 1,
    ) -> StructuredOutputResult:
        """Return output only after local validation succeeds."""
        current = request
        while True:
            budget = self._usage.check_dispatch(
                current.run_id,
                token_budget=token_budget,
                required_tokens=minimum_call_tokens,
            )
            if not budget.allowed:
                assert budget.failure is not None
                return _budget_failure(current, budget.failure)

            result = await self._router.execute(current)
            if isinstance(result, NodeExecutionFailure):
                if result.provider_invoked:
                    self._record_usage(current, result.usage, False, token_budget)
                return result

            violations = (
                validate_output(result.output, current.output_schema)
                if current.output_schema is not None
                else ()
            )
            state = self._record_usage(current, result.usage, not violations, token_budget)
            if not violations:
                return result

            failure = NodeExecutionFailure(
                category="invalid_output",
                retryable=repair_enabled,
                message="provider output does not match outputSchema",
                provider=result.provider,
                model=result.model,
                attempt=current.attempt,
                usage=result.usage,
                latency_ms=result.latency_ms,
                provider_invoked=True,
            )
            elapsed = self._elapsed_seconds()
            remaining_deadline = (
                None
                if deadline_remaining_seconds is None
                else max(deadline_remaining_seconds - elapsed, 0)
            )
            decision = schedule_retry(
                failure,
                self._retry_policy,
                elapsed_seconds=elapsed,
                cancelled=cancelled,
                deadline_remaining_seconds=remaining_deadline,
                remaining_token_budget=state.remaining_tokens,
            )
            if not decision.retry:
                return StructuredOutputFailure(decision.failure, violations)
            assert decision.next_attempt is not None
            current = replace(
                current,
                attempt=decision.next_attempt,
                remaining_token_budget=state.remaining_tokens,
                repair_errors=tuple(
                    f"{violation.path}: {violation.message}" for violation in violations
                ),
            )

    def _record_usage(
        self,
        request: NodeExecutionRequest,
        usage: TokenUsage,
        succeeded: bool,
        token_budget: int,
    ) -> RunUsageState:
        provider, _, model = (request.model or request.default_model or "").partition(":")
        return self._usage.record_attempt(
            run_id=request.run_id,
            idempotency_key=f"{request.node_id}:attempt:{request.attempt}",
            node_id=request.node_id,
            attempt=request.attempt,
            provider=provider,
            model=model,
            succeeded=succeeded,
            usage=usage,
            token_budget=token_budget,
        )


def _json_pointer(path: Any) -> str:
    parts = [str(part).replace("~", "~0").replace("/", "~1") for part in path]
    return "/" + "/".join(parts)


def _budget_failure(
    request: NodeExecutionRequest,
    failure: BudgetFailure,
) -> NodeExecutionFailure:
    provider, _, model = (request.model or request.default_model or "").partition(":")
    return NodeExecutionFailure(
        category="budget",
        retryable=False,
        message=(
            f"{failure.message}; current={failure.current}, "
            f"remaining={failure.remaining}, limit={failure.limit}"
        ),
        provider=provider or None,
        model=model or None,
        attempt=request.attempt,
    )
