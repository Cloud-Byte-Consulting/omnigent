from dataclasses import asdict
from typing import Any

import pytest

from omnigent.flow.providers import (
    AdapterRegistration,
    AdapterRequest,
    AdapterResponse,
    NodeExecutionFailure,
    NodeExecutionRequest,
    NodeExecutionSuccess,
    ProviderAdapterError,
    ProviderCapabilities,
    ProviderRegistry,
    ProviderRouter,
    TokenUsage,
)


class FakeAdapter:
    def __init__(self, response: AdapterResponse | Exception | None = None) -> None:
        self.calls: list[tuple[AdapterRequest, str]] = []
        self.response = response or AdapterResponse(output={"answer": 42})

    async def execute(self, request: AdapterRequest, *, credential: str) -> AdapterResponse:
        self.calls.append((request, credential))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def registration(
    adapter: FakeAdapter,
    *,
    provider: str = "fake",
    models: frozenset[str] = frozenset({"alpha"}),
    enabled: bool = True,
    credential_reference: str | None = "fake-key",
    tools: bool = True,
    structured_output: bool = True,
) -> AdapterRegistration:
    return AdapterRegistration(
        provider=provider,
        models=models,
        credential_reference=credential_reference,
        capabilities=ProviderCapabilities(
            tools=tools,
            structured_output=structured_output,
            usage_reporting=True,
        ),
        enabled=enabled,
        adapter=adapter,
    )


def request(**changes: Any) -> NodeExecutionRequest:
    values: dict[str, Any] = {
        "run_id": "run-1",
        "node_id": "A",
        "instructions": "Answer",
        "model": "fake:alpha",
        "default_model": "fake:beta",
        "allowed_tools": (),
        "dependency_outputs": {},
        "output_schema": None,
        "remaining_token_budget": 100,
        "attempt": 1,
    }
    values.update(changes)
    return NodeExecutionRequest(**values)


@pytest.mark.parametrize(
    ("case_request", "case_registration", "credentials", "message"),
    [
        (request(model=None, default_model=None), None, {}, "model selection is required"),
        (request(model="unknown:model"), None, {}, "model is not registered"),
        (
            request(),
            {"enabled": False},
            {"fake-key": "secret"},
            "model is disabled",
        ),
        (request(), {}, {}, "credential is not configured"),
        (
            request(allowed_tools=("search",)),
            {"tools": False},
            {"fake-key": "secret"},
            "adapter does not support tools",
        ),
        (
            request(output_schema={"type": "object"}),
            {"structured_output": False},
            {"fake-key": "secret"},
            "adapter does not support structured output",
        ),
    ],
)
async def test_rejects_invalid_configuration_before_invocation(
    case_request: NodeExecutionRequest,
    case_registration: dict[str, Any] | None,
    credentials: dict[str, str],
    message: str,
) -> None:
    adapter = FakeAdapter()
    registrations = (
        [] if case_registration is None else [registration(adapter, **case_registration)]
    )
    router = ProviderRouter(ProviderRegistry(registrations), credentials=credentials)

    result = await router.execute(case_request)

    assert result == NodeExecutionFailure(
        category="configuration",
        retryable=False,
        message=message,
        provider=None if case_request.model is None else case_request.model.partition(":")[0],
        model=None if case_request.model is None else case_request.model.partition(":")[2],
        attempt=1,
    )
    assert adapter.calls == []


async def test_explicit_model_takes_precedence_and_records_selection() -> None:
    alpha = FakeAdapter(
        AdapterResponse(
            output={"answer": "alpha"},
            usage=TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5),
            latency_ms=12,
        )
    )
    beta = FakeAdapter()
    router = ProviderRouter(
        ProviderRegistry(
            [
                registration(alpha),
                registration(beta, models=frozenset({"beta"})),
            ]
        ),
        credentials={"fake-key": "secret"},
    )

    result = await router.execute(request())

    assert result == NodeExecutionSuccess(
        output={"answer": "alpha"},
        provider="fake",
        model="alpha",
        usage=TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5),
        latency_ms=12,
        attempt=1,
        warnings=(),
    )
    assert len(alpha.calls) == 1
    assert beta.calls == []


async def test_default_model_is_used_when_node_omits_model() -> None:
    adapter = FakeAdapter()
    router = ProviderRouter(
        ProviderRegistry([registration(adapter, models=frozenset({"beta"}))]),
        credentials={"fake-key": "secret"},
    )

    result = await router.execute(request(model=None))

    assert isinstance(result, NodeExecutionSuccess)
    assert (result.provider, result.model) == ("fake", "beta")
    assert len(adapter.calls) == 1


def test_registry_rejects_ambiguous_model_routes() -> None:
    with pytest.raises(ValueError, match="duplicate model route fake:alpha"):
        ProviderRegistry([registration(FakeAdapter()), registration(FakeAdapter())])


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"provider": ""}, "provider keys must be non-empty"),
        ({"provider": "bad:key"}, "provider keys must be non-empty"),
        ({"models": frozenset()}, "must register at least one model"),
        ({"models": frozenset({"bad:model"})}, "model identifiers must be non-empty"),
    ],
)
def test_registry_rejects_invalid_configuration(
    changes: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ProviderRegistry([registration(FakeAdapter(), **changes)])


async def test_routing_is_stable_for_a_configuration_snapshot() -> None:
    adapter = FakeAdapter()
    credentials = {"fake-key": "original-secret"}
    router = ProviderRouter(
        ProviderRegistry([registration(adapter)]),
        credentials=credentials,
    )
    credentials["fake-key"] = "changed-secret"

    first = await router.execute(request())
    second = await router.execute(request())

    assert first == second
    assert adapter.calls == [
        (adapter.calls[0][0], "original-secret"),
        (adapter.calls[0][0], "original-secret"),
    ]


async def test_adapter_exception_is_safe_and_does_not_expose_credential() -> None:
    credential = "top-secret-value"
    adapter = FakeAdapter(ProviderAdapterError(f"provider rejected {credential}"))
    router = ProviderRouter(
        ProviderRegistry([registration(adapter)]),
        credentials={"fake-key": credential},
    )

    result = await router.execute(request())

    assert isinstance(result, NodeExecutionFailure)
    assert result.category == "permanent"
    assert result.retryable is False
    assert result.message == "provider invocation failed"
    assert credential not in repr(result)
    assert credential not in repr(asdict(result))
    assert credential not in repr(router)
