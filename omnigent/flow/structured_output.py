"""Local JSON Schema enforcement and bounded repair for Flow nodes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, TypeAlias

from jsonschema import Draft202012Validator

from omnigent.flow.providers import (
    AttemptAudit,
    FailureCategory,
    NodeExecutionFailure,
    NodeExecutionRequest,
    NodeExecutionSuccess,
    ProviderRouter,
    RetryPolicy,
    TokenUsage,
    schedule_retry,
)
from omnigent.flow.usage import BudgetFailure, RunUsageState, UsageRecord, UsageService

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
        attempt_history: list[AttemptAudit] = []
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
                    _, usage_record = self._record_usage(
                        current, result.usage, False, token_budget
                    )
                    attempt_history.append(_attempt_audit(usage_record, result.category))
                return replace(result, attempt_history=tuple(attempt_history))

            violations = (
                validate_output(result.output, current.output_schema)
                if current.output_schema is not None
                else ()
            )
            state, usage_record = self._record_usage(
                current, result.usage, not violations, token_budget
            )
            attempt_history.append(
                _attempt_audit(
                    usage_record,
                    "invalid_output" if violations else None,
                )
            )
            if not violations:
                return replace(result, attempt_history=tuple(attempt_history))

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
                attempt_history=tuple(attempt_history),
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
    ) -> tuple[RunUsageState, UsageRecord]:
        provider, _, model = (request.model or request.default_model or "").partition(":")
        idempotency_key = f"{request.node_id}:attempt:{request.attempt}"
        state = self._usage.record_attempt(
            run_id=request.run_id,
            idempotency_key=idempotency_key,
            node_id=request.node_id,
            attempt=request.attempt,
            provider=provider,
            model=model,
            succeeded=succeeded,
            usage=usage,
            token_budget=token_budget,
        )
        record = next(item for item in state.records if item.idempotency_key == idempotency_key)
        return state, record


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


def _attempt_audit(
    record: UsageRecord,
    category: FailureCategory | None,
) -> AttemptAudit:
    return AttemptAudit(
        attempt=record.attempt,
        provider=record.provider,
        model=record.model,
        succeeded=record.succeeded,
        usage=TokenUsage(
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            total_tokens=record.total_tokens,
        ),
        estimated=record.estimated,
        category=category,
    )
