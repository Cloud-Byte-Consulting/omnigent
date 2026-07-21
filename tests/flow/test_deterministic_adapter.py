import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass

from dapr.clients.exceptions import DaprInternalError

from omnigent.flow.e2e_provider import (
    DaprDeterministicAdapter,
    deterministic_registration,
)
from omnigent.flow.providers import (
    AdapterRequest,
    NodeExecutionFailure,
    NodeExecutionRequest,
    ProviderAdapterError,
    ProviderRegistry,
    ProviderRouter,
)
from omnigent.flow.smoke_worker import build_runtime


@dataclass
class StateResponse:
    data: bytes
    etag: str


class FakeDaprStateClient:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], tuple[bytes, int]] = {}

    def get_state(self, store_name: str, key: str) -> StateResponse:
        value, version = self.values.get((store_name, key), (b"", 0))
        return StateResponse(value, str(version) if version else "")

    def save_state(self, store_name, key, value, *, etag, options):
        del options
        current, version = self.values.get((store_name, key), (b"", 0))
        del current
        expected = str(version) if version else None
        if etag != expected:
            raise DaprInternalError("etag mismatch")
        encoded = value.encode() if isinstance(value, str) else value
        self.values[(store_name, key)] = (encoded, version + 1)


class RecordingRuntime:
    def __init__(self) -> None:
        self.workflows: list[str | None] = []
        self.activities: list[str | None] = []

    def register_workflow(self, handler, *, name=None):
        del handler
        self.workflows.append(name)

    def register_activity(self, handler, *, name=None):
        del handler
        self.activities.append(name)


def _request(node_id: str, *, dependencies=None) -> AdapterRequest:
    return AdapterRequest(
        run_id="run-1",
        node_id=node_id,
        instructions=f"execute {node_id}",
        model="deterministic",
        allowed_tools=(),
        dependency_outputs=dependencies or {},
        output_schema=None,
        remaining_token_budget=3,
        attempt=1,
        node_execution_id=f"stable-{node_id}",
    )


def test_redelivery_reuses_one_logical_side_effect_and_cached_output() -> None:
    client = FakeDaprStateClient()
    adapter = DaprDeterministicAdapter(client)

    first = asyncio.run(adapter.execute(_request("A"), credential="fixture"))
    second = asyncio.run(adapter.execute(_request("A"), credential="fixture"))

    assert first.output == {"value": "A"}
    assert second.output == first.output
    record = adapter.effect("stable-A")
    assert record.delivery_count == 2
    assert record.effect_count == 1
    assert record.completed is True
    assert record.output == {"value": "A"}


def test_incomplete_delivery_is_completed_once_on_redelivery() -> None:
    client = FakeDaprStateClient()
    adapter = DaprDeterministicAdapter(client, slow_node="B", delay_seconds=0.05)

    async def interrupt_first_delivery() -> None:
        task = asyncio.create_task(adapter.execute(_request("B"), credential="fixture"))
        await asyncio.sleep(0.01)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    asyncio.run(interrupt_first_delivery())
    interrupted = adapter.effect("stable-B")
    assert interrupted.delivery_count == 1
    assert interrupted.effect_count == 1
    assert interrupted.completed is False

    completed = asyncio.run(adapter.execute(_request("B"), credential="fixture"))

    assert completed.output == {"value": "B"}
    recovered = adapter.effect("stable-B")
    assert recovered.delivery_count == 2
    assert recovered.effect_count == 1
    assert recovered.completed is True


def test_join_node_receives_both_dependency_results_in_stable_order() -> None:
    adapter = DaprDeterministicAdapter(FakeDaprStateClient())

    response = asyncio.run(
        adapter.execute(
            _request("C", dependencies={"B": {"value": "B"}, "A": {"value": "A"}}),
            credential="fixture",
        )
    )

    assert response.output == {"values": ["A", "B"]}
    assert response.usage.total_tokens == 1


def test_configured_invalid_node_returns_durable_schema_mismatch_fixture() -> None:
    adapter = DaprDeterministicAdapter(
        FakeDaprStateClient(),
        invalid_node="FAIL",
    )

    first = asyncio.run(adapter.execute(_request("FAIL"), credential="fixture"))
    retry = asyncio.run(adapter.execute(_request("FAIL"), credential="fixture"))

    assert first.output == {"invalid": "FAIL"}
    assert retry.output == first.output
    assert adapter.effect("stable-FAIL").delivery_count == 2


def test_corrupt_persisted_effect_fails_closed() -> None:
    client = FakeDaprStateClient()
    client.values[("flowstatestore", "flow-fake-effect:stable-A")] = (
        json.dumps({"nodeExecutionId": "wrong"}).encode(),
        1,
    )
    adapter = DaprDeterministicAdapter(client)

    try:
        asyncio.run(adapter.execute(_request("A"), credential="fixture"))
    except ProviderAdapterError as error:
        assert error.code == "invalid_state"
    else:
        raise AssertionError("corrupt state must not be overwritten")


class UnavailableDaprStateClient:
    def get_state(self, store_name: str, key: str) -> StateResponse:
        del store_name, key
        raise DaprInternalError("secret backend details")


async def test_dapr_failure_is_normalized_by_the_runtime_registration() -> None:
    adapter = DaprDeterministicAdapter(UnavailableDaprStateClient())
    router = ProviderRouter(
        ProviderRegistry([deterministic_registration(adapter)]),
        credentials={"fixture-credential": "local-only"},
    )

    result = await router.execute(
        NodeExecutionRequest(
            run_id="run-1",
            node_id="A",
            instructions="execute A",
            model="fake:deterministic",
            default_model=None,
            allowed_tools=(),
            dependency_outputs={},
            output_schema=None,
            remaining_token_budget=3,
            attempt=1,
            node_execution_id="stable-A",
        )
    )

    assert isinstance(result, NodeExecutionFailure)
    assert result.category == "transient"
    assert result.retryable is True
    assert result.message == "deterministic state store is temporarily unavailable"
    assert "secret backend details" not in repr(result)


def test_worker_registers_smoke_and_dag_workflows_with_node_activity() -> None:
    runtime = RecordingRuntime()

    build_runtime(runtime=runtime, state_client=FakeDaprStateClient())

    assert runtime.workflows == ["FlowRuntimeSmoke", "FlowDagWorkflow"]
    assert runtime.activities == ["ExecuteFlowNode"]
