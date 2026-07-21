import json
import os
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from omnigent.flow.e2e_provider import (
    DaprDeterministicAdapter,
    deterministic_registration,
)
from omnigent.flow.providers import (
    AdapterRegistration,
    AdapterRequest,
    AdapterResponse,
    NodeExecutionFailure,
    NodeExecutionRequest,
    NodeExecutionSuccess,
    ProviderAdapterError,
    ProviderCapabilities,
    ProviderFailureRule,
    ProviderRegistry,
    ProviderRouter,
    RetryPolicy,
    TokenUsage,
)
from omnigent.flow.structured_output import StructuredOutputRunner
from omnigent.flow.usage import ConservativeUsagePolicy, InMemoryUsageStore, UsageService

MATRIX_PATH = Path(__file__).parent / "fixtures" / "providers" / "matrix.json"
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _matrix() -> dict[str, Any]:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _enabled_adapters() -> list[dict[str, Any]]:
    return [adapter for adapter in _matrix()["adapters"] if adapter["enabled"]]


class ScriptedAdapter:
    def __init__(self, outcomes: Iterator[AdapterResponse | ProviderAdapterError]) -> None:
        self._outcomes = outcomes
        self.calls: list[tuple[AdapterRequest, str]] = []

    async def execute(self, request: AdapterRequest, *, credential: str) -> AdapterResponse:
        self.calls.append((request, credential))
        outcome = next(self._outcomes)
        if isinstance(outcome, ProviderAdapterError):
            raise outcome
        return outcome


class UnusedStateClient:
    def get_state(self, store_name: str, key: str):
        del store_name, key
        raise AssertionError("matrix comparison must not execute the adapter")

    def save_state(self, store_name, key, value, *, etag, options):
        del store_name, key, value, etag, options
        raise AssertionError("matrix comparison must not execute the adapter")


def _registration(
    adapter: ScriptedAdapter,
    entry: dict[str, Any],
) -> AdapterRegistration:
    capabilities = entry["capabilities"]
    return AdapterRegistration(
        provider=entry["provider"],
        models=frozenset({entry["model"]}),
        credential_reference="contract-secret",
        capabilities=ProviderCapabilities(
            tools=capabilities["tools"],
            structured_output=capabilities["structuredOutput"],
            usage_reporting=capabilities["usageReporting"],
        ),
        enabled=True,
        adapter=adapter,
        error_mapping={
            "temporary": ProviderFailureRule(
                category="transient",
                retryable=True,
                safe_message="provider temporarily unavailable",
            ),
            "rejected": ProviderFailureRule(
                category="permanent",
                retryable=False,
                safe_message="provider rejected the request",
            ),
        },
    )


def _request(entry: dict[str, Any], **changes: Any) -> NodeExecutionRequest:
    values: dict[str, Any] = {
        "run_id": f"contract-{entry['provider']}",
        "node_id": "A",
        "instructions": "Return an integer answer",
        "model": f"{entry['provider']}:{entry['model']}",
        "default_model": None,
        "allowed_tools": (),
        "dependency_outputs": {},
        "output_schema": OUTPUT_SCHEMA,
        "remaining_token_budget": 100,
        "attempt": 1,
        "node_execution_id": "stable-A",
    }
    values.update(changes)
    return NodeExecutionRequest(**values)


def _router(adapter: ScriptedAdapter, entry: dict[str, Any], *, secret: str = "secret"):
    return ProviderRouter(
        ProviderRegistry([_registration(adapter, entry)]),
        credentials={"contract-secret": secret},
    )


def test_provider_matrix_is_versioned_complete_and_non_billable_by_default() -> None:
    matrix = _matrix()

    assert matrix["schemaVersion"] == "1.0"
    assert matrix["realProviderGate"] == {
        "environmentVariable": "FLOW_REAL_PROVIDER_TESTS",
        "enabledValue": "1",
    }
    assert matrix["adapters"]
    for adapter in matrix["adapters"]:
        assert set(adapter) == {
            "provider",
            "model",
            "implementation",
            "version",
            "verifiedAt",
            "enabled",
            "testMode",
            "billable",
            "credentialEnvironmentVariable",
            "capabilities",
            "limitations",
        }
        assert set(adapter["capabilities"]) == {
            "tools",
            "structuredOutput",
            "usageReporting",
        }
        if adapter["enabled"]:
            assert adapter["testMode"] == "deterministic"
            assert adapter["billable"] is False
            assert adapter["credentialEnvironmentVariable"] is None


def test_enabled_matrix_matches_the_runtime_registration() -> None:
    entries = _enabled_adapters()
    assert len(entries) == 1
    entry = entries[0]
    registration = deterministic_registration(
        DaprDeterministicAdapter(UnusedStateClient())
    )

    assert f"{registration.provider}:{next(iter(registration.models))}" == (
        f"{entry['provider']}:{entry['model']}"
    )
    assert registration.enabled is entry["enabled"]
    assert registration.capabilities == ProviderCapabilities(
        tools=entry["capabilities"]["tools"],
        structured_output=entry["capabilities"]["structuredOutput"],
        usage_reporting=entry["capabilities"]["usageReporting"],
    )


@pytest.mark.parametrize("entry", _enabled_adapters(), ids=lambda item: item["provider"])
async def test_enabled_adapter_routes_and_preserves_normalized_usage(entry) -> None:
    adapter = ScriptedAdapter(
        iter(
            (
                AdapterResponse(
                    output={"answer": 42},
                    usage=TokenUsage(input_tokens=7, output_tokens=3, total_tokens=10),
                    latency_ms=12,
                    warnings=("normalized",),
                ),
            )
        )
    )

    result = await _router(adapter, entry).execute(_request(entry))

    assert result == NodeExecutionSuccess(
        output={"answer": 42},
        provider=entry["provider"],
        model=entry["model"],
        usage=TokenUsage(input_tokens=7, output_tokens=3, total_tokens=10),
        latency_ms=12,
        attempt=1,
        warnings=("normalized",),
    )
    routed, credential = adapter.calls[0]
    assert credential == "secret"
    assert routed.node_execution_id == "stable-A"
    assert routed.output_schema == OUTPUT_SCHEMA


@pytest.mark.parametrize("entry", _enabled_adapters(), ids=lambda item: item["provider"])
async def test_enabled_adapter_repairs_structured_output_and_accounts_for_each_attempt(
    entry,
) -> None:
    adapter = ScriptedAdapter(
        iter(
            (
                AdapterResponse(output={"answer": "wrong"}, usage=TokenUsage(total_tokens=4)),
                AdapterResponse(output={"answer": 42}, usage=TokenUsage(total_tokens=5)),
            )
        )
    )
    store = InMemoryUsageStore()
    runner = StructuredOutputRunner(
        _router(adapter, entry),
        UsageService(store, missing_usage_policy=ConservativeUsagePolicy(10)),
        retry_policy=RetryPolicy(2, 60, 0),
        elapsed_seconds=lambda: 0,
    )

    result = await runner.execute(_request(entry), token_budget=100)

    assert isinstance(result, NodeExecutionSuccess)
    assert result.output == {"answer": 42}
    usage = store.state(f"contract-{entry['provider']}", token_budget=100)
    assert [record.total_tokens for record in usage.records] == [4, 5]
    assert [record.succeeded for record in usage.records] == [False, True]
    assert adapter.calls[1][0].repair_errors


@pytest.mark.parametrize("entry", _enabled_adapters(), ids=lambda item: item["provider"])
@pytest.mark.parametrize(
    ("code", "category", "retryable"),
    [("temporary", "transient", True), ("rejected", "permanent", False)],
)
async def test_enabled_adapter_normalizes_errors_without_raw_payloads(
    entry,
    code: str,
    category: str,
    retryable: bool,
) -> None:
    secret = "sk-contract-secret"
    adapter = ScriptedAdapter(iter((ProviderAdapterError(code, f"raw payload {secret}"),)))

    result = await _router(adapter, entry, secret=secret).execute(_request(entry))

    assert isinstance(result, NodeExecutionFailure)
    assert result.category == category
    assert result.retryable is retryable
    assert result.provider_invoked is True
    serialized = repr(asdict(result))
    assert secret not in serialized
    assert "raw payload" not in serialized


@pytest.mark.parametrize("entry", _enabled_adapters(), ids=lambda item: item["provider"])
async def test_capability_variance_is_enforced_and_recorded(entry) -> None:
    adapter = ScriptedAdapter(iter((AdapterResponse(output={"answer": 42}),)))
    result = await _router(adapter, entry).execute(
        _request(entry, allowed_tools=("search",), output_schema=None)
    )

    if entry["capabilities"]["tools"]:
        assert isinstance(result, NodeExecutionSuccess)
        assert "tools" not in entry["limitations"]
    else:
        assert isinstance(result, NodeExecutionFailure)
        assert result.message == "adapter does not support tools"
        assert adapter.calls == []
        assert "tools" in entry["limitations"]


def test_ambient_credentials_cannot_enable_billable_contract_tests(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-read")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-read")
    monkeypatch.delenv("FLOW_REAL_PROVIDER_TESTS", raising=False)
    matrix = _matrix()

    assert os.environ.get(matrix["realProviderGate"]["environmentVariable"]) is None
    assert all(not adapter["billable"] for adapter in _enabled_adapters())


@pytest.mark.skipif(
    os.environ.get("FLOW_REAL_PROVIDER_TESTS") != "1",
    reason="set FLOW_REAL_PROVIDER_TESTS=1 to authorize real-provider contract tests",
)
def test_opt_in_real_provider_matrix_has_no_unimplemented_claims() -> None:
    configured = [adapter for adapter in _matrix()["adapters"] if adapter["billable"]]
    if not configured:
        pytest.skip("no real provider adapter is configured yet")
    assert all(adapter["credentialEnvironmentVariable"] for adapter in configured)
