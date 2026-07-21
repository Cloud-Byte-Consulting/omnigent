"""Deterministic Dapr orchestration for an approved Flow DAG."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Generator, Mapping
from copy import deepcopy
from typing import Any, Protocol, cast

import dapr.ext.workflow as wf
from pydantic import BaseModel, ConfigDict, Field

from omnigent.flow.activity import NODE_EXECUTION_ACTIVITY_NAME, NodeActivityInput
from omnigent.flow.contracts import DagSpec, ExpansionRequest, WorkflowNode
from omnigent.flow.runtime_audit import RUNTIME_AUDIT_ACTIVITY_NAME
from omnigent.flow.runtime_caps import RUNTIME_CAP_ACTIVITY_NAME
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
    *,
    persist_audit: bool = False,
    persist_caps: bool = False,
) -> Generator[object, object, JsonObject]:
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
        if persist_audit:
            yield context.call_activity(
                RUNTIME_AUDIT_ACTIVITY_NAME,
                input=_audit_input(
                    workflow_input.run_id,
                    [
                        _audit_draft(
                            "validation",
                            correlation_key=(
                                f"{workflow_input.run_id}:round:"
                                f"{workflow_input.current_round}:validation"
                            ),
                            summary="Workflow validation rejected the DAG",
                            metadata={"valid": False},
                        ),
                        _audit_draft(
                            "run_rejected",
                            correlation_key=f"{workflow_input.run_id}:terminal:rejected",
                            summary="Workflow run was rejected",
                            metadata={"status": "rejected"},
                        ),
                    ],
                ),
            )
        return rejection

    dag = validation.dag
    nodes_by_id = {node.id: node for node in dag.nodes}
    node_states: dict[str, JsonObject] = {node.id: {"status": "pending"} for node in dag.nodes}
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

    if persist_caps:
        raw_acceptance = yield context.call_activity(
            RUNTIME_CAP_ACTIVITY_NAME,
            input=_cap_input(
                workflow_input,
                dag,
                kind="accept_nodes",
                idempotency_key=_cap_acceptance_key(workflow_input, dag),
                node_ids=[node.id for node in dag.nodes],
                round_number=workflow_input.current_round,
            ),
        )
        acceptance = _cap_decision(raw_acceptance)
        if not acceptance["allowed"]:
            rejection = _cap_rejection(context, workflow_input, node_states, events, acceptance)
            if persist_audit:
                yield context.call_activity(
                    RUNTIME_AUDIT_ACTIVITY_NAME,
                    input=_audit_input(
                        workflow_input.run_id,
                        _audit_specs_from_events(workflow_input.run_id, events),
                    ),
                )
            return rejection

    if persist_audit:
        bootstrap = [
            *_audit_specs_from_events(workflow_input.run_id, events),
            _audit_draft(
                "validation",
                correlation_key=(
                    f"{workflow_input.run_id}:round:{workflow_input.current_round}:validation"
                ),
                summary="Workflow DAG passed validation",
                metadata={"valid": True, "round": workflow_input.current_round},
            ),
            _audit_draft(
                "run_queued",
                correlation_key=f"{workflow_input.run_id}:queued",
                summary="Workflow run was queued",
                metadata={},
            ),
            _audit_draft(
                "run_running",
                correlation_key=f"{workflow_input.run_id}:running",
                summary="Workflow run started",
                metadata={"round": workflow_input.current_round},
            ),
            *(
                _audit_draft(
                    "node_queued",
                    node_id=node.id,
                    correlation_key=(
                        f"{derive_node_execution_id(workflow_input.run_id, node.id)}:queued"
                    ),
                    summary=f"Node {node.id} was queued",
                    metadata={},
                )
                for node in dag.nodes
                if node_states[node.id]["status"] != "succeeded"
            ),
        ]
        yield context.call_activity(
            RUNTIME_AUDIT_ACTIVITY_NAME,
            input=_audit_input(workflow_input.run_id, bootstrap),
        )

    used_tokens = max(workflow_input.used_tokens, restored_tokens)
    expansion_outcome: str | None = None

    batches_by_wave = dispatch_batches(
        validation.waves,
        max_concurrent=dag.caps.max_concurrent,
    )
    for initial_wave_batches in batches_by_wave:
        wave_batches = list(initial_wave_batches)
        for batch in wave_batches:
            checkpoint_start = len(events)
            tasks: list[Any] = []
            scheduled_ids: list[str] = []
            eligible_ids: list[str] = []
            retired_ids: list[str] = []
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
                    retired_ids.append(node_id)
                    continue

                eligible_ids.append(node_id)

            if persist_caps:
                yield from _release_cap_reservations(
                    context,
                    workflow_input,
                    dag,
                    retired_ids,
                )

            remaining = max(dag.caps.token_budget - used_tokens, 0)
            share, extra = divmod(remaining, len(eligible_ids)) if eligible_ids else (0, 0)
            approved_dispatches: list[tuple[str, WorkflowNode, int]] = []
            for index, node_id in enumerate(eligible_ids):
                node = nodes_by_id[node_id]
                allocated_tokens = share + (1 if index < extra else 0)
                if persist_caps:
                    try:
                        raw_cap_decision = yield context.call_activity(
                            RUNTIME_CAP_ACTIVITY_NAME,
                            input=_cap_input(
                                workflow_input,
                                dag,
                                kind="dispatch",
                                idempotency_key=_cap_node_key(
                                    workflow_input.run_id, node_id, "dispatch"
                                ),
                                node_id=node_id,
                                required_tokens=max(allocated_tokens, 1),
                            ),
                        )
                        cap_decision = _cap_decision(raw_cap_decision)
                    except Exception:
                        yield from _release_cap_reservations(
                            context,
                            workflow_input,
                            dag,
                            eligible_ids,
                        )
                        raise
                    if cap_decision["queued"]:
                        node_states[node_id] = {"status": "queued"}
                        deferred_ids = eligible_ids[index:]
                        if approved_dispatches:
                            wave_batches.append(tuple(deferred_ids))
                        else:
                            for deferred_id in deferred_ids:
                                node_states[deferred_id] = {
                                    "status": "failed",
                                    "failure": _cap_failure(cap_decision),
                                }
                            yield from _release_cap_reservations(
                                context,
                                workflow_input,
                                dag,
                                deferred_ids,
                            )
                        break
                    if not cap_decision["allowed"]:
                        node_states[node_id] = {
                            "status": "failed",
                            "failure": _cap_failure(cap_decision),
                        }
                        yield from _release_cap_reservations(
                            context,
                            workflow_input,
                            dag,
                            [node_id],
                        )
                        continue
                approved_dispatches.append((node_id, node, allocated_tokens))

            for node_id, node, allocated_tokens in approved_dispatches:
                activity_input = _activity_input(
                    workflow_input,
                    dag,
                    node,
                    outputs,
                    remaining_token_budget=allocated_tokens,
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

            if persist_audit and len(events) > checkpoint_start:
                yield context.call_activity(
                    RUNTIME_AUDIT_ACTIVITY_NAME,
                    input=_audit_input(
                        workflow_input.run_id,
                        _audit_specs_from_events(
                            workflow_input.run_id,
                            events[checkpoint_start:],
                        ),
                    ),
                )

            _set_status(
                context,
                workflow_input,
                "running",
                node_states,
                events,
            )
            if not tasks:
                continue

            try:
                raw_results = yield join_tasks(tasks)
            except Exception:
                if persist_caps:
                    yield from _release_cap_reservations(
                        context,
                        workflow_input,
                        dag,
                        scheduled_ids,
                    )
                raise
            results = cast(list[JsonObject], raw_results)
            if len(results) != len(scheduled_ids):
                if persist_caps:
                    yield from _release_cap_reservations(
                        context,
                        workflow_input,
                        dag,
                        scheduled_ids,
                    )
                raise RuntimeError("Dapr returned an unexpected activity result count")
            if persist_caps:
                yield from _release_cap_reservations(
                    context,
                    workflow_input,
                    dag,
                    scheduled_ids,
                )
            result_checkpoint_start = len(events)
            for node_id, result in zip(scheduled_ids, results, strict=True):
                used_tokens += _usage_total(result)
                attempts = _attempt_history(result)
                for recorded_attempt in attempts:
                    recorded_number = cast(int, recorded_attempt["attempt"])
                    if recorded_number > 1:
                        _event(events, "retry", node_id, attempt=recorded_number)
                    usage = cast(JsonObject, recorded_attempt["usage"])
                    _event(
                        events,
                        "usage",
                        node_id,
                        attempt=recorded_number,
                        provider=recorded_attempt.get("provider"),
                        model=recorded_attempt.get("model"),
                        succeeded=recorded_attempt.get("succeeded"),
                        category=recorded_attempt.get("category"),
                        estimated=recorded_attempt.get("estimated"),
                        **usage,
                    )
                attempt = _attempt(result)
                if result.get("status") == "success":
                    output = deepcopy(result.get("output"))
                    outputs[node_id] = output
                    persisted_results[node_id] = deepcopy(result)
                    node_states[node_id] = {
                        "status": "succeeded",
                        "output": output,
                    }
                    _event(events, "node_succeeded", node_id, attempt=attempt)
                else:
                    failure = _failure_summary(result)
                    node_states[node_id] = {
                        "status": "failed",
                        "failure": failure,
                    }
                    if "attempt" in failure:
                        node_states[node_id]["attempt"] = failure["attempt"]
                    _event(
                        events,
                        "node_failed",
                        node_id,
                        category=failure["category"],
                        attempt=attempt,
                    )
            for node_id, result in zip(scheduled_ids, results, strict=True):
                raw_expansion = result.get("expansionRequest")
                if result.get("status") != "success" or not isinstance(raw_expansion, Mapping):
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
            if persist_audit and len(events) > result_checkpoint_start:
                yield context.call_activity(
                    RUNTIME_AUDIT_ACTIVITY_NAME,
                    input=_audit_input(
                        workflow_input.run_id,
                        _audit_specs_from_events(
                            workflow_input.run_id,
                            events[result_checkpoint_start:],
                        ),
                    ),
                )
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
    terminal_checkpoint_start = len(events)
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
    if persist_audit:
        yield context.call_activity(
            RUNTIME_AUDIT_ACTIVITY_NAME,
            input=_audit_input(
                workflow_input.run_id,
                _audit_specs_from_events(
                    workflow_input.run_id,
                    events[terminal_checkpoint_start:],
                ),
            ),
        )
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
        return (
            yield from orchestrate_dag(
                context,
                value,
                wf.when_all,
                persist_audit=True,
                persist_caps=True,
            )
        )

    runtime.register_workflow(flow_dag_workflow, name=FLOW_WORKFLOW_NAME)
    return flow_dag_workflow


def _cap_input(
    workflow_input: FlowWorkflowInput,
    dag: DagSpec,
    *,
    kind: str,
    idempotency_key: str,
    node_ids: list[str] | None = None,
    round_number: int | None = None,
    node_id: str | None = None,
    required_tokens: int = 0,
) -> JsonObject:
    value: JsonObject = {
        "runId": workflow_input.run_id,
        "limits": dag.caps.model_dump(mode="json", by_alias=True),
        "kind": kind,
        "idempotencyKey": idempotency_key,
    }
    if node_ids is not None:
        value["nodeIds"] = node_ids
    if round_number is not None:
        value["roundNumber"] = round_number
    if node_id is not None:
        value["nodeId"] = node_id
    if required_tokens:
        value["requiredTokens"] = required_tokens
    return value


def _cap_acceptance_key(workflow_input: FlowWorkflowInput, dag: DagSpec) -> str:
    canonical = json.dumps(
        {
            "runId": workflow_input.run_id,
            "round": workflow_input.current_round,
            "nodeIds": sorted(node.id for node in dag.nodes),
            "limits": dag.caps.model_dump(mode="json", by_alias=True),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"{workflow_input.run_id}:round:{workflow_input.current_round}:accept:{digest}"


def _cap_node_key(run_id: str, node_id: str, transition: str) -> str:
    return f"{derive_node_execution_id(run_id, node_id)}:cap:{transition}"


def _cap_decision(result: object) -> JsonObject:
    if not isinstance(result, Mapping) or not isinstance(result.get("decision"), Mapping):
        raise RuntimeError("cap activity returned an invalid result")
    raw = cast(Mapping[str, Any], result["decision"])
    if not isinstance(raw.get("allowed"), bool) or not isinstance(raw.get("queued"), bool):
        raise RuntimeError("cap activity returned an invalid decision")
    for key in ("current", "proposed", "limit"):
        if not isinstance(raw.get(key), int) or isinstance(raw.get(key), bool):
            raise RuntimeError("cap activity returned invalid utilization")
    return deepcopy(dict(raw))


def _release_cap_reservations(
    context: WorkflowContext,
    workflow_input: FlowWorkflowInput,
    dag: DagSpec,
    node_ids: list[str],
) -> Generator[object, object, None]:
    for node_id in node_ids:
        completion = yield context.call_activity(
            RUNTIME_CAP_ACTIVITY_NAME,
            input=_cap_input(
                workflow_input,
                dag,
                kind="complete",
                idempotency_key=_cap_node_key(workflow_input.run_id, node_id, "complete"),
                node_id=node_id,
            ),
        )
        _cap_decision(completion)


def _cap_failure(decision: Mapping[str, Any]) -> JsonObject:
    cap = decision.get("cap")
    return {
        "category": "budget" if cap == "tokenBudget" else "configuration",
        "retryable": False,
        "message": (
            f"runtime cap denied dispatch; cap={cap}; current={decision['current']}; "
            f"proposed={decision['proposed']}; limit={decision['limit']}"
        ),
    }


def _cap_rejection(
    context: WorkflowContext,
    workflow_input: FlowWorkflowInput,
    node_states: dict[str, JsonObject],
    events: list[JsonObject],
    decision: Mapping[str, Any],
) -> JsonObject:
    _event(
        events,
        "cap_denial",
        cap=decision.get("cap"),
        current=decision["current"],
        proposed=decision["proposed"],
        limit=decision["limit"],
        round=workflow_input.current_round,
    )
    _event(events, "run_completed", status="rejected")
    result: JsonObject = {
        "runId": workflow_input.run_id,
        "approvedDagDigest": workflow_input.approved_dag_digest,
        "status": "rejected",
        "nodes": node_states,
        "events": events,
        "capDecision": deepcopy(dict(decision)),
    }
    context.set_custom_status(_canonical_json(result))
    return result


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
    return sum(
        cast(int, attempt["usage"].get("totalTokens", 0)) for attempt in _attempt_history(result)
    )


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
            round=workflow_input.current_round,
            currentRound=workflow_input.current_round,
            usedTokens=used_tokens,
            **usage,
            errorCodes=[error.code for error in validation.errors],
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
        event.get("type") == event_type and event.get("nodeId") == node_id for event in events
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
    summary: JsonObject = {
        "category": category if isinstance(category, str) else "permanent",
        "retryable": failure.get("retryable") is True,
        "message": message if isinstance(message, str) else "node execution failed",
    }
    attempt = failure.get("attempt")
    if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0:
        summary["attempt"] = attempt
    return summary


def _attempt(result: Mapping[str, Any]) -> int:
    value: object = result.get("attempt", 1)
    if result.get("status") == "failure" and isinstance(result.get("failure"), Mapping):
        value = cast(Mapping[str, Any], result["failure"]).get("attempt", value)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1


def _safe_usage(result: Mapping[str, Any]) -> JsonObject | None:
    value: object = result.get("usage")
    if result.get("status") == "failure" and isinstance(result.get("failure"), Mapping):
        value = cast(Mapping[str, Any], result["failure"]).get("usage")
    if not isinstance(value, Mapping):
        return None
    usage = {
        key: item
        for key in ("inputTokens", "outputTokens", "totalTokens")
        if isinstance((item := value.get(key)), int) and not isinstance(item, bool) and item >= 0
    }
    return usage or None


def _attempt_history(result: Mapping[str, Any]) -> list[JsonObject]:
    raw_history = result.get("attemptHistory")
    history: list[JsonObject] = []
    if isinstance(raw_history, list):
        for item in raw_history:
            if not isinstance(item, Mapping):
                continue
            attempt = item.get("attempt")
            usage = item.get("usage")
            if (
                not isinstance(attempt, int)
                or isinstance(attempt, bool)
                or attempt <= 0
                or not isinstance(usage, Mapping)
            ):
                continue
            safe_usage = {
                key: value
                for key in ("inputTokens", "outputTokens", "totalTokens")
                if isinstance((value := usage.get(key)), int)
                and not isinstance(value, bool)
                and value >= 0
            }
            if not safe_usage:
                continue
            history.append(
                {
                    "attempt": attempt,
                    "provider": item.get("provider")
                    if isinstance(item.get("provider"), str)
                    else None,
                    "model": item.get("model") if isinstance(item.get("model"), str) else None,
                    "succeeded": item.get("succeeded") is True,
                    "category": item.get("category")
                    if isinstance(item.get("category"), str)
                    else None,
                    "estimated": item.get("estimated") is True,
                    "usage": safe_usage,
                }
            )
    if history:
        return history
    usage = _safe_usage(result)
    if usage is None:
        return []
    failure = result.get("failure")
    failure_map = failure if isinstance(failure, Mapping) else {}
    return [
        {
            "attempt": _attempt(result),
            "provider": result.get("provider", failure_map.get("provider")),
            "model": result.get("model", failure_map.get("model")),
            "succeeded": result.get("status") == "success",
            "category": failure_map.get("category"),
            "estimated": False,
            "usage": usage,
        }
    ]


def _audit_input(run_id: str, events: list[JsonObject]) -> JsonObject:
    return {"runId": run_id, "events": events}


def _audit_draft(
    event_type: str,
    *,
    correlation_key: str,
    summary: str,
    metadata: Mapping[str, Any],
    node_id: str | None = None,
) -> JsonObject:
    return {
        "type": event_type,
        "nodeId": node_id,
        "source": "workflow",
        "correlationKey": correlation_key,
        "summary": summary,
        "metadata": deepcopy(dict(metadata)),
    }


def _audit_specs_from_events(run_id: str, events: list[JsonObject]) -> list[JsonObject]:
    specs: list[JsonObject] = []
    safe_metadata = {
        "attempt",
        "category",
        "round",
        "decision",
        "idempotencyKey",
        "cap",
        "current",
        "proposed",
        "limit",
        "status",
        "usedTokens",
        "inputTokens",
        "outputTokens",
        "totalTokens",
        "provider",
        "model",
        "blockedBy",
        "succeeded",
        "estimated",
        "errorCodes",
    }
    for event in events:
        event_type = event.get("type")
        node_id = event.get("nodeId") if isinstance(event.get("nodeId"), str) else None
        sequence = event.get("sequence") if isinstance(event.get("sequence"), int) else 0
        metadata = {key: event[key] for key in safe_metadata if key in event}
        identity = derive_node_execution_id(run_id, node_id) if node_id is not None else run_id
        attempt = metadata.get("attempt") if isinstance(metadata.get("attempt"), int) else 1

        if event_type == "node_scheduled" and node_id is not None:
            specs.extend(
                (
                    _audit_draft(
                        "dispatch",
                        node_id=node_id,
                        correlation_key=f"{identity}:dispatch",
                        summary=f"Node {node_id} was dispatched",
                        metadata={"attempt": attempt},
                    ),
                    _audit_draft(
                        "node_running",
                        node_id=node_id,
                        correlation_key=f"{identity}:attempt:{attempt}:running",
                        summary=f"Node {node_id} started",
                        metadata={"attempt": attempt},
                    ),
                )
            )
        elif event_type in {"node_succeeded", "node_failed"} and node_id is not None:
            specs.append(
                _audit_draft(
                    event_type,
                    node_id=node_id,
                    correlation_key=f"{identity}:{event_type.removeprefix('node_')}",
                    summary=f"Node {node_id} {event_type.removeprefix('node_')}",
                    metadata=metadata,
                )
            )
        elif event_type == "node_blocked" and node_id is not None:
            specs.append(
                _audit_draft(
                    "node_blocked",
                    node_id=node_id,
                    correlation_key=f"{identity}:blocked",
                    summary=f"Node {node_id} was blocked",
                    metadata=metadata,
                )
            )
        elif event_type in {"retry", "usage"} and node_id is not None:
            specs.append(
                _audit_draft(
                    event_type,
                    node_id=node_id,
                    correlation_key=f"{identity}:attempt:{attempt}:{event_type}",
                    summary=f"Node {node_id} recorded {event_type}",
                    metadata=metadata,
                )
            )
        elif event_type in {"expansion", "expansion_rejected", "cap_denial"}:
            correlation = metadata.get("idempotencyKey")
            specs.append(
                _audit_draft(
                    event_type,
                    node_id=node_id,
                    correlation_key=(
                        str(correlation)
                        if isinstance(correlation, str) and correlation
                        else f"{run_id}:event:{sequence}:{event_type}"
                    ),
                    summary=f"Workflow recorded {event_type.replace('_', ' ')}",
                    metadata=metadata,
                )
            )
        elif event_type == "run_completed":
            status = metadata.get("status")
            terminal_type = (
                {
                    "succeeded": "run_succeeded",
                    "failed": "run_failed",
                    "rejected": "run_rejected",
                }.get(status)
                if isinstance(status, str)
                else None
            )
            if terminal_type is not None:
                specs.append(
                    _audit_draft(
                        terminal_type,
                        correlation_key=f"{run_id}:terminal:{status}",
                        summary=f"Workflow run {status}",
                        metadata=metadata,
                    )
                )
    return specs


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
