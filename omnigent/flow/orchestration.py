"""Deterministic Dapr orchestration for an approved Flow DAG."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Generator, Mapping
from copy import deepcopy
from typing import Any, Protocol

import dapr.ext.workflow as wf
from pydantic import BaseModel, ConfigDict, Field

from omnigent.flow.activity import NODE_EXECUTION_ACTIVITY_NAME, NodeActivityInput
from omnigent.flow.contracts import DagSpec, WorkflowNode
from omnigent.flow.validation import dispatch_batches, validate_dag

FLOW_WORKFLOW_NAME = "FlowDagWorkflow"
JsonObject = dict[str, Any]


def _camel_case(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class FlowWorkflowInput(BaseModel):
    """Approved, replay-safe input to the durable DAG workflow."""

    model_config = ConfigDict(
        alias_generator=_camel_case,
        extra="forbid",
        populate_by_name=True,
        strict=True,
    )

    run_id: str = Field(min_length=1)
    approved_dag_digest: str = Field(min_length=1)
    dag_spec: DagSpec
    persisted_results: dict[str, JsonObject] = Field(default_factory=dict)

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Use public JSON names when the Dapr SDK serializes this model."""
        kwargs.setdefault("by_alias", True)
        return super().model_dump(*args, **kwargs)


class WorkflowContext(Protocol):
    def call_activity(self, activity: str, *, input: JsonObject) -> object: ...

    def set_custom_status(self, value: str) -> None: ...


class WorkflowRuntime(Protocol):
    def register_workflow(
        self,
        fn: Callable[..., Generator[object, object, JsonObject]],
        *,
        name: str | None = None,
    ) -> object: ...


JoinTasks = Callable[[list[Any]], object]


def derive_node_execution_id(run_id: str, node_id: str) -> str:
    """Derive a stable logical side-effect identity from the run and node."""
    if not run_id or not node_id:
        raise ValueError("run_id and node_id are required")
    canonical = json.dumps([run_id, node_id], ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def orchestrate_dag(
    context: WorkflowContext,
    workflow_input: FlowWorkflowInput,
    join_tasks: JoinTasks,
) -> Generator[object, list[JsonObject], JsonObject]:
    """Schedule deterministic bounded batches and return provider-neutral run state."""
    validation = validate_dag(workflow_input.dag_spec)
    if not validation.is_valid or validation.dag is None:
        result = {
            "runId": workflow_input.run_id,
            "approvedDagDigest": workflow_input.approved_dag_digest,
            "status": "rejected",
            "errors": [
                {"code": error.code, "path": error.path, "message": error.message}
                for error in validation.errors
            ],
            "nodes": {},
            "events": [],
        }
        context.set_custom_status(_canonical_json(result))
        return result

    dag = validation.dag
    nodes_by_id = {node.id: node for node in dag.nodes}
    node_states: dict[str, JsonObject] = {
        node.id: {"status": "pending"} for node in dag.nodes
    }
    outputs: dict[str, Any] = {}
    events: list[JsonObject] = []
    used_tokens = 0

    for node in dag.nodes:
        persisted = workflow_input.persisted_results.get(node.id)
        if persisted is None or persisted.get("status") != "success":
            continue
        output = deepcopy(persisted.get("output"))
        outputs[node.id] = output
        node_states[node.id] = {"status": "succeeded", "output": output}
        used_tokens += _usage_total(persisted)
        _event(events, "node_restored", node.id)

    batches_by_wave = dispatch_batches(
        validation.waves,
        max_concurrent=dag.caps.max_concurrent,
    )
    for wave_batches in batches_by_wave:
        for batch in wave_batches:
            tasks: list[Any] = []
            scheduled_ids: list[str] = []
            eligible_ids: list[str] = []
            for node_id in batch:
                if node_states[node_id]["status"] == "succeeded":
                    continue
                node = nodes_by_id[node_id]
                blocked_by = [
                    dependency
                    for dependency in node.depends_on
                    if node_states[dependency]["status"] != "succeeded"
                ]
                if blocked_by:
                    node_states[node_id] = {
                        "status": "blocked",
                        "blockedBy": blocked_by,
                    }
                    _event(events, "node_blocked", node_id, blockedBy=blocked_by)
                    continue

                eligible_ids.append(node_id)

            remaining = max(dag.caps.token_budget - used_tokens, 0)
            share, extra = divmod(remaining, len(eligible_ids)) if eligible_ids else (0, 0)
            for index, node_id in enumerate(eligible_ids):
                node = nodes_by_id[node_id]
                activity_input = _activity_input(
                    workflow_input,
                    dag,
                    node,
                    outputs,
                    remaining_token_budget=share + (1 if index < extra else 0),
                )
                node_states[node_id] = {"status": "running", "attempt": 1}
                _event(events, "node_scheduled", node_id, attempt=1)
                tasks.append(
                    context.call_activity(
                        NODE_EXECUTION_ACTIVITY_NAME,
                        input=activity_input,
                    )
                )
                scheduled_ids.append(node_id)

            _set_status(
                context,
                workflow_input,
                "running",
                node_states,
                events,
            )
            if not tasks:
                continue

            results = yield join_tasks(tasks)
            if len(results) != len(scheduled_ids):
                raise RuntimeError("Dapr returned an unexpected activity result count")
            for node_id, result in zip(scheduled_ids, results, strict=True):
                used_tokens += _usage_total(result)
                if result.get("status") == "success":
                    output = deepcopy(result.get("output"))
                    outputs[node_id] = output
                    node_states[node_id] = {
                        "status": "succeeded",
                        "output": output,
                    }
                    _event(events, "node_succeeded", node_id)
                else:
                    failure = _failure_summary(result)
                    node_states[node_id] = {
                        "status": "failed",
                        "failure": failure,
                    }
                    _event(
                        events,
                        "node_failed",
                        node_id,
                        category=failure["category"],
                    )
            _set_status(
                context,
                workflow_input,
                "running",
                node_states,
                events,
            )

    status = (
        "succeeded"
        if all(value["status"] == "succeeded" for value in node_states.values())
        else "failed"
    )
    _event(events, "run_completed", status=status)
    result = {
        "runId": workflow_input.run_id,
        "approvedDagDigest": workflow_input.approved_dag_digest,
        "status": status,
        "nodes": node_states,
        "events": events,
        "usedTokens": used_tokens,
    }
    context.set_custom_status(
        _canonical_json(_status_snapshot(workflow_input, status, node_states, events))
    )
    return result


def register_flow_workflow(
    runtime: WorkflowRuntime,
) -> Callable[[wf.DaprWorkflowContext, FlowWorkflowInput], Generator[object, object, JsonObject]]:
    """Register the deterministic workflow with Dapr Workflow 1.18.0."""

    def flow_dag_workflow(
        context: wf.DaprWorkflowContext,
        workflow_input: FlowWorkflowInput,
    ) -> Generator[object, object, JsonObject]:
        value = (
            workflow_input
            if isinstance(workflow_input, FlowWorkflowInput)
            else FlowWorkflowInput.model_validate(workflow_input)
        )
        return (yield from orchestrate_dag(context, value, wf.when_all))

    runtime.register_workflow(flow_dag_workflow, name=FLOW_WORKFLOW_NAME)
    return flow_dag_workflow


def _activity_input(
    workflow_input: FlowWorkflowInput,
    dag: DagSpec,
    node: WorkflowNode,
    outputs: Mapping[str, Any],
    *,
    remaining_token_budget: int,
) -> JsonObject:
    value = NodeActivityInput(
        nodeExecutionId=derive_node_execution_id(workflow_input.run_id, node.id),
        runId=workflow_input.run_id,
        nodeId=node.id,
        instructions=node.instructions,
        model=node.model,
        defaultModel=dag.default_model,
        tools=list(node.tools or []),
        dependsOn=list(node.depends_on),
        dependencyOutputs={
            dependency: deepcopy(outputs[dependency]) for dependency in node.depends_on
        },
        outputSchema=deepcopy(node.output_schema),
        remainingTokenBudget=remaining_token_budget,
        tokenBudget=dag.caps.token_budget,
        attempt=1,
    )
    return value.model_dump(mode="json", by_alias=True)


def _usage_total(result: Mapping[str, Any]) -> int:
    usage = result.get("usage")
    if result.get("status") == "failure":
        failure = result.get("failure")
        usage = failure.get("usage") if isinstance(failure, Mapping) else None
    if not isinstance(usage, Mapping):
        return 0
    total = usage.get("totalTokens")
    return total if isinstance(total, int) and total >= 0 else 0


def _failure_summary(result: Mapping[str, Any]) -> JsonObject:
    failure = result.get("failure")
    if not isinstance(failure, Mapping):
        return {
            "category": "permanent",
            "retryable": False,
            "message": "activity returned an invalid failure result",
        }
    category = failure.get("category")
    message = failure.get("message")
    return {
        "category": category if isinstance(category, str) else "permanent",
        "retryable": failure.get("retryable") is True,
        "message": message if isinstance(message, str) else "node execution failed",
    }


def _event(
    events: list[JsonObject],
    event_type: str,
    node_id: str | None = None,
    **details: Any,
) -> None:
    event: JsonObject = {
        "sequence": len(events) + 1,
        "type": event_type,
    }
    if node_id is not None:
        event["nodeId"] = node_id
    event.update(deepcopy(details))
    events.append(event)


def _set_status(
    context: WorkflowContext,
    workflow_input: FlowWorkflowInput,
    status: str,
    node_states: Mapping[str, JsonObject],
    events: list[JsonObject],
) -> None:
    context.set_custom_status(
        _canonical_json(_status_snapshot(workflow_input, status, node_states, events))
    )


def _status_snapshot(
    workflow_input: FlowWorkflowInput,
    status: str,
    node_states: Mapping[str, JsonObject],
    events: list[JsonObject],
) -> JsonObject:
    summarized_nodes = {
        node_id: {
            key: deepcopy(value)
            for key, value in state.items()
            if key in {"status", "attempt", "blockedBy", "failure"}
        }
        for node_id, state in node_states.items()
    }
    return {
        "runId": workflow_input.run_id,
        "approvedDagDigest": workflow_input.approved_dag_digest,
        "status": status,
        "nodes": summarized_nodes,
        "events": deepcopy(events),
    }


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
