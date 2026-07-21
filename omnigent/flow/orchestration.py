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
from omnigent.flow.contracts import DagSpec, ExpansionRequest, WorkflowNode
from omnigent.flow.validation import (
    ContractError,
    dispatch_batches,
    validate_dag,
    validate_expansion,
)

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
    current_round: int = Field(default=1, gt=0)
    used_tokens: int = Field(default=0, ge=0)
    applied_expansions: list[str] = Field(default_factory=list)
    persisted_events: list[JsonObject] = Field(default_factory=list)

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Use public JSON names when the Dapr SDK serializes this model."""
        kwargs.setdefault("by_alias", True)
        return super().model_dump(*args, **kwargs)


class WorkflowContext(Protocol):
    def call_activity(self, activity: str, *, input: JsonObject) -> object: ...

    def set_custom_status(self, value: str) -> None: ...

    def continue_as_new(self, new_input: JsonObject, *, save_events: bool = False) -> None: ...


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
        rejection: JsonObject = {
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
        context.set_custom_status(_canonical_json(rejection))
        return rejection

    dag = validation.dag
    nodes_by_id = {node.id: node for node in dag.nodes}
    node_states: dict[str, JsonObject] = {
        node.id: {"status": "pending"} for node in dag.nodes
    }
    outputs: dict[str, Any] = {}
    events: list[JsonObject] = deepcopy(workflow_input.persisted_events)
    persisted_results = deepcopy(workflow_input.persisted_results)
    restored_tokens = 0

    for node in dag.nodes:
        persisted = workflow_input.persisted_results.get(node.id)
        if persisted is None or persisted.get("status") != "success":
            continue
        output = deepcopy(persisted.get("output"))
        outputs[node.id] = output
        node_states[node.id] = {"status": "succeeded", "output": output}
        restored_tokens += _usage_total(persisted)
        if not _has_node_event(events, "node_succeeded", node.id):
            _event(events, "node_restored", node.id)

    used_tokens = max(workflow_input.used_tokens, restored_tokens)
    expansion_outcome: str | None = None

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
                    persisted_results[node_id] = deepcopy(result)
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
            for node_id, result in zip(scheduled_ids, results, strict=True):
                raw_expansion = result.get("expansionRequest")
                if result.get("status") != "success" or not isinstance(
                    raw_expansion, Mapping
                ):
                    continue
                continuation, rejected_status = _apply_expansion(
                    context,
                    workflow_input,
                    dag,
                    raw_expansion,
                    succeeded_node_ids={node_id},
                    persisted_results=persisted_results,
                    events=events,
                    used_tokens=used_tokens,
                )
                if continuation is not None:
                    return continuation
                if rejected_status is not None:
                    expansion_outcome = rejected_status
            _set_status(
                context,
                workflow_input,
                "running",
                node_states,
                events,
            )

    status = (
        "rejected"
        if expansion_outcome is not None
        else "succeeded"
        if all(value["status"] == "succeeded" for value in node_states.values())
        else "failed"
    )
    _event(events, "run_completed", status=status)
    run_result: JsonObject = {
        "runId": workflow_input.run_id,
        "approvedDagDigest": workflow_input.approved_dag_digest,
        "status": status,
        "nodes": node_states,
        "events": events,
        "usedTokens": used_tokens,
    }
    snapshot = _status_snapshot(workflow_input, status, node_states, events)
    if expansion_outcome is not None:
        run_result["expansionStatus"] = expansion_outcome
        snapshot["expansionStatus"] = expansion_outcome
    context.set_custom_status(_canonical_json(snapshot))
    return run_result


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
        node_execution_id=derive_node_execution_id(workflow_input.run_id, node.id),
        run_id=workflow_input.run_id,
        node_id=node.id,
        instructions=node.instructions,
        model=node.model,
        default_model=dag.default_model,
        tools=list(node.tools or []),
        depends_on=list(node.depends_on),
        dependency_outputs={
            dependency: deepcopy(outputs[dependency]) for dependency in node.depends_on
        },
        output_schema=deepcopy(node.output_schema),
        remaining_token_budget=remaining_token_budget,
        token_budget=dag.caps.token_budget,
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


def _apply_expansion(
    context: WorkflowContext,
    workflow_input: FlowWorkflowInput,
    dag: DagSpec,
    raw_expansion: Mapping[str, Any],
    *,
    succeeded_node_ids: set[str],
    persisted_results: dict[str, JsonObject],
    events: list[JsonObject],
    used_tokens: int,
) -> tuple[JsonObject | None, str | None]:
    validation = validate_expansion(
        dag,
        raw_expansion,
        succeeded_node_ids=succeeded_node_ids,
        current_round=workflow_input.current_round,
        tokens_used=used_tokens,
    )
    expansion_key = _expansion_key(raw_expansion)
    if expansion_key in workflow_input.applied_expansions:
        _event(events, "expansion_replayed", idempotencyKey=expansion_key)
        return None, None
    if validation.dag is None:
        cap = _expansion_cap(validation.errors)
        usage = _cap_usage(cap, dag, raw_expansion, workflow_input.current_round, used_tokens)
        _event(
            events,
            "cap_denial" if cap is not None else "expansion_rejected",
            cap=cap,
            currentRound=workflow_input.current_round,
            usedTokens=used_tokens,
            **usage,
            errors=[
                {"code": error.code, "path": error.path, "message": error.message}
                for error in validation.errors
            ],
        )
        return None, "cap_reached" if cap is not None else "rejected"

    request = ExpansionRequest.model_validate(raw_expansion)
    _event(
        events,
        "expansion",
        request.node_id,
        round=request.round,
        decision="accepted",
        idempotencyKey=expansion_key,
    )
    continuation = FlowWorkflowInput(
        run_id=workflow_input.run_id,
        approved_dag_digest=workflow_input.approved_dag_digest,
        dag_spec=validation.dag,
        persisted_results=persisted_results,
        current_round=request.round,
        used_tokens=used_tokens,
        applied_expansions=[*workflow_input.applied_expansions, expansion_key],
        persisted_events=events,
    )
    serialized = continuation.model_dump(mode="json")
    context.set_custom_status(
        _canonical_json(
            {
                "runId": workflow_input.run_id,
                "approvedDagDigest": workflow_input.approved_dag_digest,
                "status": "continuing",
                "currentRound": request.round,
                "events": deepcopy(events),
            }
        )
    )
    context.continue_as_new(serialized, save_events=False)
    return (
        {
            "runId": workflow_input.run_id,
            "approvedDagDigest": workflow_input.approved_dag_digest,
            "status": "continued",
            "currentRound": request.round,
            "events": deepcopy(events),
            "usedTokens": used_tokens,
        },
        None,
    )


def _expansion_key(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _expansion_cap(errors: tuple[ContractError, ...]) -> str | None:
    mapping = {
        "max_nodes_exceeded": "maxNodes",
        "max_rounds_exceeded": "maxRounds",
        "token_budget_exceeded": "tokenBudget",
    }
    return next((mapping[error.code] for error in errors if error.code in mapping), None)


def _cap_usage(
    cap: str | None,
    dag: DagSpec,
    expansion: Mapping[str, Any],
    current_round: int,
    used_tokens: int,
) -> JsonObject:
    if cap == "maxNodes":
        nodes = expansion.get("nodes")
        proposed_nodes = len(nodes) if isinstance(nodes, list) else 0
        return {
            "current": len(dag.nodes),
            "proposed": len(dag.nodes) + proposed_nodes,
            "limit": dag.caps.max_nodes,
        }
    if cap == "maxRounds":
        proposed_round = expansion.get("round")
        return {
            "current": current_round,
            "proposed": proposed_round if isinstance(proposed_round, int) else current_round,
            "limit": dag.caps.max_rounds,
        }
    if cap == "tokenBudget":
        return {
            "current": used_tokens,
            "proposed": used_tokens,
            "limit": dag.caps.token_budget,
        }
    return {}


def _has_node_event(events: list[JsonObject], event_type: str, node_id: str) -> bool:
    return any(
        event.get("type") == event_type and event.get("nodeId") == node_id
        for event in events
    )


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
