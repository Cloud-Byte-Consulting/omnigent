"""Local Dapr worker for smoke checks and the deterministic Flow E2E harness."""

from __future__ import annotations

import os
import signal
import threading
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any, cast

import dapr.ext.workflow as wf
from dapr.clients import DaprClient

from omnigent.flow.activity import NodeExecutionActivity, register_node_execution_activity
from omnigent.flow.audit import DaprAuditStore
from omnigent.flow.caps import DaprCapStore
from omnigent.flow.e2e_provider import (
    DaprDeterministicAdapter,
    DaprStateClient,
    ExpansionFixtureNodeActivity,
    deterministic_registration,
)
from omnigent.flow.orchestration import register_flow_workflow
from omnigent.flow.providers import ProviderRegistry, ProviderRouter, RetryPolicy
from omnigent.flow.runtime_audit import RuntimeAuditActivity, register_runtime_audit_activity
from omnigent.flow.runtime_caps import RuntimeCapActivity, register_runtime_cap_activity
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
    invalid_node: str | None = None,
    expansion_node: str | None = None,
    delay_seconds: float = 0,
) -> Any:
    """Register every workflow/activity needed by local verification."""
    runtime.register_workflow(smoke_workflow, name="FlowRuntimeSmoke")
    register_flow_workflow(runtime)

    adapter = DaprDeterministicAdapter(
        state_client,
        slow_node=slow_node,
        invalid_node=invalid_node,
        delay_seconds=delay_seconds,
    )
    router = ProviderRouter(
        ProviderRegistry([deterministic_registration(adapter)]),
        credentials={"fixture-credential": "local-only"},
    )
    usage = UsageService(
        DaprUsageStore(state_client),
        missing_usage_policy=ConservativeUsagePolicy(1),
    )
    audit = DaprAuditStore(state_client)
    cap_store = DaprCapStore(state_client)
    runner = StructuredOutputRunner(
        router,
        usage,
        retry_policy=RetryPolicy(
            max_attempts=2,
            max_elapsed_seconds=60,
            initial_delay_seconds=0,
        ),
        elapsed_seconds=lambda: 0,
    )
    register_runtime_audit_activity(runtime, RuntimeAuditActivity(audit))
    register_runtime_cap_activity(
        runtime,
        RuntimeCapActivity(
            cap_store,
            usage=usage,
            audit=audit,
            clock=lambda: datetime.now(UTC),
        ),
    )
    node_activity = NodeExecutionActivity(runner)
    if expansion_node:
        node_activity = ExpansionFixtureNodeActivity(
            node_activity,
            proposer_node=expansion_node,
        )
    register_node_execution_activity(runtime, node_activity)
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
        state_client=cast(DaprStateClient, state_client),
        slow_node=os.environ.get("FLOW_FAKE_SLOW_NODE") or None,
        invalid_node=os.environ.get("FLOW_FAKE_INVALID_NODE") or None,
        expansion_node=os.environ.get("FLOW_FAKE_EXPANSION_NODE") or None,
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
