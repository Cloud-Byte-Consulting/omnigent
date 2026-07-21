"""Provider-neutral model routing for Flow node execution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from math import isfinite
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeAlias

JsonObject: TypeAlias = Mapping[str, Any]
FailureCategory: TypeAlias = Literal[
    "configuration",
    "authentication",
    "rate_limit",
    "transient",
    "invalid_output",
    "budget",
    "permanent",
]


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Features a registered adapter can safely provide."""

    tools: bool
    structured_output: bool
    usage_reporting: bool


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Provider-neutral token accounting when reported by an adapter."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class NodeExecutionRequest:
    """Provider-neutral input accepted by the router."""

    run_id: str
    node_id: str
    instructions: str
    model: str | None
    default_model: str | None
    allowed_tools: tuple[str, ...]
    dependency_outputs: JsonObject
    output_schema: JsonObject | None
    remaining_token_budget: int
    attempt: int


@dataclass(frozen=True, slots=True)
class AdapterRequest:
    """Resolved provider-neutral request passed to one adapter."""

    run_id: str
    node_id: str
    instructions: str
    model: str
    allowed_tools: tuple[str, ...]
    dependency_outputs: JsonObject
    output_schema: JsonObject | None
    remaining_token_budget: int
    attempt: int


@dataclass(frozen=True, slots=True)
class AdapterResponse:
    """Provider-neutral success returned by an adapter."""

    output: Any
    usage: TokenUsage = TokenUsage()
    latency_ms: int | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NodeExecutionSuccess:
    """Normalized successful node execution."""

    output: Any
    provider: str
    model: str
    usage: TokenUsage
    latency_ms: int | None
    attempt: int
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NodeExecutionFailure:
    """Normalized safe failure returned across the workflow boundary."""

    category: FailureCategory
    retryable: bool
    message: str
    provider: str | None
    model: str | None
    attempt: int
    request_id: str | None = None
    retry_after_seconds: float | None = None
    usage: TokenUsage = TokenUsage()
    latency_ms: int | None = None


NodeExecutionResult: TypeAlias = NodeExecutionSuccess | NodeExecutionFailure


@dataclass(frozen=True, slots=True)
class ProviderFailureRule:
    """Configured mapping from one adapter code to a safe category."""

    category: FailureCategory
    retryable: bool
    safe_message: str

    def __post_init__(self) -> None:
        if not self.safe_message:
            raise ValueError("safe_message is required")


class ProviderAdapterError(Exception):
    """Adapter failure carrying raw details that never cross the router."""

    def __init__(
        self,
        code: str,
        raw_message: str = "",
        *,
        request_id: str | None = None,
        retry_after_seconds: float | None = None,
        usage: TokenUsage | None = None,
        latency_ms: int | None = None,
    ) -> None:
        super().__init__(raw_message)
        if not code:
            raise ValueError("provider error code is required")
        if retry_after_seconds is not None and (
            retry_after_seconds < 0 or not isfinite(retry_after_seconds)
        ):
            raise ValueError("retry_after_seconds must be finite and non-negative")
        if latency_ms is not None and latency_ms < 0:
            raise ValueError("latency_ms cannot be negative")
        self.code = code
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds
        self.usage = usage or TokenUsage()
        self.latency_ms = latency_ms


class ProviderAdapter(Protocol):
    """Stable adapter boundary implemented by provider integrations."""

    async def execute(self, request: AdapterRequest, *, credential: str) -> AdapterResponse: ...


@dataclass(frozen=True, slots=True)
class AdapterRegistration:
    """One immutable provider configuration snapshot entry."""

    provider: str
    models: frozenset[str]
    credential_reference: str | None
    capabilities: ProviderCapabilities
    enabled: bool
    adapter: ProviderAdapter
    error_mapping: Mapping[str, ProviderFailureRule] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if any(not code for code in self.error_mapping):
            raise ValueError("provider error mapping codes must be non-empty")
        object.__setattr__(self, "error_mapping", MappingProxyType(dict(self.error_mapping)))


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Run-scoped limits for scheduling another provider attempt."""

    max_attempts: int
    max_elapsed_seconds: float
    initial_delay_seconds: float
    multiplier: float = 2
    max_delay_seconds: float = 60

    def __post_init__(self) -> None:
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if self.max_elapsed_seconds <= 0 or not isfinite(self.max_elapsed_seconds):
            raise ValueError("max_elapsed_seconds must be positive")
        if self.initial_delay_seconds < 0 or not isfinite(self.initial_delay_seconds):
            raise ValueError("initial_delay_seconds cannot be negative")
        if self.multiplier < 1 or not isfinite(self.multiplier):
            raise ValueError("multiplier must be at least 1")
        if self.max_delay_seconds < self.initial_delay_seconds or not isfinite(
            self.max_delay_seconds
        ):
            raise ValueError("max_delay_seconds cannot be less than the initial delay")


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """A scheduled next attempt or a terminal normalized failure."""

    retry: bool
    delay_seconds: float | None
    next_attempt: int | None
    failure: NodeExecutionFailure


class ProviderRegistry:
    """Deterministic exact-match registry for provider:model references."""

    def __init__(self, registrations: Sequence[AdapterRegistration]) -> None:
        routes: dict[str, AdapterRegistration] = {}
        for registration in registrations:
            if not registration.provider or ":" in registration.provider:
                raise ValueError("provider keys must be non-empty and cannot contain ':'")
            if not registration.models:
                raise ValueError(
                    f"provider {registration.provider!r} must register at least one model"
                )
            for model in sorted(registration.models):
                if not model or ":" in model:
                    raise ValueError("model identifiers must be non-empty and cannot contain ':'")
                reference = f"{registration.provider}:{model}"
                if reference in routes:
                    raise ValueError(f"duplicate model route {reference}")
                routes[reference] = registration
        self._routes = MappingProxyType(routes)

    def resolve(self, reference: str) -> AdapterRegistration | None:
        """Resolve one exact provider:model reference from this snapshot."""
        return self._routes.get(reference)


class ProviderRouter:
    """Resolve configuration and invoke exactly one capable adapter."""

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        credentials: Mapping[str, str],
    ) -> None:
        self._registry = registry
        self._credentials = MappingProxyType(dict(credentials))

    async def execute(self, request: NodeExecutionRequest) -> NodeExecutionResult:
        """Route one node request, returning only a normalized result."""
        reference = request.model if request.model is not None else request.default_model
        provider, model = _reference_parts(reference)
        if reference is None:
            return _configuration_failure(request, "model selection is required", provider, model)

        registration = self._registry.resolve(reference)
        if registration is None:
            return _configuration_failure(request, "model is not registered", provider, model)
        if not registration.enabled:
            return _configuration_failure(request, "model is disabled", provider, model)

        credential = (
            self._credentials.get(registration.credential_reference)
            if registration.credential_reference is not None
            else None
        )
        if not credential:
            return _configuration_failure(
                request,
                "credential is not configured",
                provider,
                model,
            )
        if request.allowed_tools and not registration.capabilities.tools:
            return _configuration_failure(
                request,
                "adapter does not support tools",
                provider,
                model,
            )
        if request.output_schema is not None and not registration.capabilities.structured_output:
            return _configuration_failure(
                request,
                "adapter does not support structured output",
                provider,
                model,
            )

        adapter_request = AdapterRequest(
            run_id=request.run_id,
            node_id=request.node_id,
            instructions=request.instructions,
            model=model or "",
            allowed_tools=request.allowed_tools,
            dependency_outputs=request.dependency_outputs,
            output_schema=request.output_schema,
            remaining_token_budget=request.remaining_token_budget,
            attempt=request.attempt,
        )
        try:
            response = await registration.adapter.execute(adapter_request, credential=credential)
        except ProviderAdapterError as error:
            return _normalize_provider_error(
                error,
                registration,
                provider=provider,
                model=model,
                attempt=request.attempt,
            )
        return NodeExecutionSuccess(
            output=response.output,
            provider=provider or "",
            model=model or "",
            usage=response.usage,
            latency_ms=response.latency_ms,
            attempt=request.attempt,
            warnings=response.warnings,
        )


def _reference_parts(reference: str | None) -> tuple[str | None, str | None]:
    if reference is None:
        return None, None
    provider, separator, model = reference.partition(":")
    if not separator:
        return provider or None, None
    return provider or None, model or None


def _configuration_failure(
    request: NodeExecutionRequest,
    message: str,
    provider: str | None,
    model: str | None,
) -> NodeExecutionFailure:
    return NodeExecutionFailure(
        category="configuration",
        retryable=False,
        message=message,
        provider=provider,
        model=model,
        attempt=request.attempt,
    )


def schedule_retry(
    failure: NodeExecutionFailure,
    policy: RetryPolicy,
    *,
    elapsed_seconds: float,
    cancelled: bool = False,
    deadline_remaining_seconds: float | None = None,
    remaining_token_budget: int | None = None,
) -> RetryDecision:
    """Apply run policy and provider guidance to one normalized failure."""
    if elapsed_seconds < 0 or not isfinite(elapsed_seconds):
        raise ValueError("elapsed_seconds cannot be negative")
    if deadline_remaining_seconds is not None and (
        deadline_remaining_seconds < 0 or not isfinite(deadline_remaining_seconds)
    ):
        raise ValueError("deadline_remaining_seconds cannot be negative")

    if (
        not failure.retryable
        or cancelled
        or failure.attempt >= policy.max_attempts
        or elapsed_seconds >= policy.max_elapsed_seconds
        or (remaining_token_budget is not None and remaining_token_budget <= 0)
    ):
        return _terminal(failure)

    backoff = policy.initial_delay_seconds
    for _ in range(max(failure.attempt - 1, 0)):
        backoff = min(backoff * policy.multiplier, policy.max_delay_seconds)
        if backoff == policy.max_delay_seconds:
            break
    delay = max(backoff, failure.retry_after_seconds or 0)
    if elapsed_seconds + delay >= policy.max_elapsed_seconds or (
        deadline_remaining_seconds is not None and delay >= deadline_remaining_seconds
    ):
        return _terminal(failure)
    return RetryDecision(
        retry=True,
        delay_seconds=delay,
        next_attempt=failure.attempt + 1,
        failure=failure,
    )


def _normalize_provider_error(
    error: ProviderAdapterError,
    registration: AdapterRegistration,
    *,
    provider: str | None,
    model: str | None,
    attempt: int,
) -> NodeExecutionFailure:
    rule = registration.error_mapping.get(error.code)
    if rule is None:
        rule = ProviderFailureRule(
            category="permanent",
            retryable=False,
            safe_message="provider invocation failed",
        )
    return NodeExecutionFailure(
        category=rule.category,
        retryable=rule.retryable,
        message=rule.safe_message,
        provider=provider,
        model=model,
        attempt=attempt,
        request_id=_safe_request_id(error.request_id),
        retry_after_seconds=error.retry_after_seconds,
        usage=error.usage,
        latency_ms=error.latency_ms,
    )


def _safe_request_id(value: str | None) -> str | None:
    if value is None or not value or len(value) > 200:
        return None
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:")
    lowered = value.lower()
    if lowered.startswith("sk-") or "bearer" in lowered or "api_key" in lowered:
        return None
    return value if all(character in allowed for character in value) else None


def _terminal(failure: NodeExecutionFailure) -> RetryDecision:
    return RetryDecision(
        retry=False,
        delay_seconds=None,
        next_attempt=None,
        failure=replace(failure, retryable=False),
    )
