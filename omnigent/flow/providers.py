"""Provider-neutral model routing for Flow node execution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


NodeExecutionResult: TypeAlias = NodeExecutionSuccess | NodeExecutionFailure


class ProviderAdapterError(Exception):
    """Provider failure that is safe for the router to normalize."""


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
        except ProviderAdapterError:
            return NodeExecutionFailure(
                category="permanent",
                retryable=False,
                message="provider invocation failed",
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
