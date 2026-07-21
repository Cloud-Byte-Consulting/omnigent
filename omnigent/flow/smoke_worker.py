"""Local Dapr worker for smoke checks and the deterministic Flow E2E harness."""

from __future__ import annotations

import os
import signal
import threading
from collections.abc import Generator
from typing import Any

import dapr.ext.workflow as wf
from dapr.clients import DaprClient

from omnigent.flow.activity import NodeExecutionActivity, register_node_execution_activity
from omnigent.flow.e2e_provider import (
    DaprDeterministicAdapter,
    DaprStateClient,
    deterministic_registration,
)
from omnigent.flow.orchestration import register_flow_workflow
from omnigent.flow.providers import ProviderRegistry, ProviderRouter, RetryPolicy
from omnigent.flow.structured_output import StructuredOutputRunner
from omnigent.flow.usage import ConservativeUsagePolicy, DaprUsageStore, UsageService


def smoke_workflow(
    ctx: wf.DaprWorkflowContext,
    workflow_input: object,
) -> Generator[object, object, object]:
    """Wait for an event so local lifecycle controls have a durable target."""
    del workflow_input
    result = yield ctx.wait_for_external_event("complete")
    return result


def build_runtime(
    *,
    runtime: Any,
    state_client: DaprStateClient,
    slow_node: str | None = None,
    delay_seconds: float = 0,
) -> Any:
    """Register every workflow/activity needed by local verification."""
    runtime.register_workflow(smoke_workflow, name="FlowRuntimeSmoke")
    register_flow_workflow(runtime)

    adapter = DaprDeterministicAdapter(
        state_client,
        slow_node=slow_node,
        delay_seconds=delay_seconds,
    )
    router = ProviderRouter(
        ProviderRegistry([deterministic_registration(adapter)]),
        credentials={"fixture-credential": "local-only"},
    )
    runner = StructuredOutputRunner(
        router,
        UsageService(
            DaprUsageStore(state_client),
            missing_usage_policy=ConservativeUsagePolicy(1),
        ),
        retry_policy=RetryPolicy(
            max_attempts=1,
            max_elapsed_seconds=60,
            initial_delay_seconds=0,
        ),
        elapsed_seconds=lambda: 0,
    )
    register_node_execution_activity(runtime, NodeExecutionActivity(runner))
    return runtime


def main() -> None:
    stopped = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    delay_seconds = _delay_seconds(os.environ.get("FLOW_FAKE_DELAY_SECONDS", "0"))
    state_client = DaprClient()
    runtime = build_runtime(
        runtime=wf.WorkflowRuntime(),
        state_client=state_client,
        slow_node=os.environ.get("FLOW_FAKE_SLOW_NODE") or None,
        delay_seconds=delay_seconds,
    )
    runtime.start()
    try:
        stopped.wait()
    finally:
        runtime.shutdown()
        state_client.close()


def _delay_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError("FLOW_FAKE_DELAY_SECONDS must be numeric") from error
    if parsed < 0:
        raise ValueError("FLOW_FAKE_DELAY_SECONDS cannot be negative")
    return parsed


if __name__ == "__main__":
    main()
