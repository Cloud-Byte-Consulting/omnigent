import json
from dataclasses import dataclass
from typing import Any

import pytest

from omnigent.flow.orchestration import (
    FLOW_WORKFLOW_NAME,
    FlowWorkflowInput,
    derive_node_execution_id,
    orchestrate_dag,
)


def workflow_input(*, max_concurrent: int = 2, nodes: list[dict[str, Any]] | None = None) -> dict:
    return {
        "runId": "run-1",
        "approvedDagDigest": "sha256:approved",
        "dagSpec": {
            "version": "1.0",
            "nodes": nodes
            or [
                {"id": "A", "instructions": "A", "model": "fake:alpha"},
                {"id": "B", "instructions": "B", "model": "fake:alpha"},
                {
                    "id": "C",
                    "instructions": "C",
                    "dependsOn": ["A", "B"],
                    "model": "fake:alpha",
                },
            ],
            "caps": {
                "maxNodes": 10,
                "maxRounds": 2,
                "maxConcurrent": max_concurrent,
                "tokenBudget": 100,
            },
        },
    }


@dataclass
class FakeTask:
    activity: str
    input: dict[str, Any]


@dataclass
class Joined:
    tasks: list[FakeTask]


class FakeContext:
    def __init__(self) -> None:
        self.calls: list[FakeTask] = []
        self.custom_statuses: list[dict[str, Any]] = []
        self.continuations: list[dict[str, Any]] = []

    def call_activity(self, activity: str, *, input: dict[str, Any]) -> FakeTask:
        task = FakeTask(activity, input)
        self.calls.append(task)
        return task

    def set_custom_status(self, value: str) -> None:
        self.custom_statuses.append(json.loads(value))

    def continue_as_new(self, value: dict[str, Any], *, save_events: bool = False) -> None:
        assert save_events is False
        self.continuations.append(value)


def success(node_id: str, output: object) -> dict[str, Any]:
    return {
        "status": "success",
        "output": output,
        "provider": "fake",
        "model": "alpha",
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        "latencyMs": 1,
        "attempt": 1,
        "warnings": [],
        "nodeId": node_id,
    }


def failure(category: str = "permanent") -> dict[str, Any]:
    return {
        "status": "failure",
        "failure": {
            "category": category,
            "retryable": False,
            "message": "safe failure",
            "attempt": 2,
            "provider": "fake",
            "model": "alpha",
            "usage": {"inputTokens": 1, "outputTokens": 0, "totalTokens": 1},
        },
        "attemptHistory": [
            {
                "attempt": 1,
                "provider": "fake",
                "model": "alpha",
                "succeeded": False,
                "category": category,
                "usage": {"inputTokens": 1, "outputTokens": 0, "totalTokens": 1},
            },
            {
                "attempt": 2,
                "provider": "fake",
                "model": "alpha",
                "succeeded": False,
                "category": category,
                "usage": {"inputTokens": 1, "outputTokens": 0, "totalTokens": 1},
            },
        ],
    }


def join(tasks: list[FakeTask]) -> Joined:
    return Joined(tasks)


def cap_result(*, allowed: bool = True, queued: bool = False) -> dict[str, Any]:
    return {
        "decision": {
            "allowed": allowed,
            "queued": queued,
            "cap": None if allowed else "tokenBudget",
            "current": 0,
            "proposed": 1,
            "limit": 100,
            "message": "cap decision",
        },
        "state": {},
    }


def finish(generator: object, value: object) -> dict[str, Any]:
    try:
        generator.send(value)  # type: ignore[attr-defined]
    except StopIteration as stopped:
        return stopped.value
    raise AssertionError("workflow did not complete")


def test_fan_out_and_fan_in_schedule_after_persisted_successes() -> None:
    context = FakeContext()
    generator = orchestrate_dag(context, FlowWorkflowInput.model_validate(workflow_input()), join)

    first = next(generator)

    assert [task.input["nodeId"] for task in first.tasks] == ["A", "B"]
    assert all(task.activity == "ExecuteFlowNode" for task in first.tasks)
    assert all(task.input["dependencyOutputs"] == {} for task in first.tasks)

    second = generator.send([success("A", {"a": 1}), success("B", {"b": 2})])

    assert [task.input["nodeId"] for task in second.tasks] == ["C"]
    assert second.tasks[0].input["dependencyOutputs"] == {
        "A": {"a": 1},
        "B": {"b": 2},
    }
    result = finish(generator, [success("C", {"final": 3})])
    assert result["status"] == "succeeded"
    assert [result["nodes"][node]["status"] for node in ("A", "B", "C")] == [
        "succeeded",
        "succeeded",
        "succeeded",
    ]


def test_wide_wave_is_dispatched_in_deterministic_bounded_batches() -> None:
    nodes = [
        {"id": node_id, "instructions": node_id, "model": "fake:alpha"}
        for node_id in ("A", "B", "C", "D", "E")
    ]
    context = FakeContext()
    generator = orchestrate_dag(
        context,
        FlowWorkflowInput.model_validate(workflow_input(max_concurrent=2, nodes=nodes)),
        join,
    )
    batch_sizes: list[int] = []
    task = next(generator)
    while True:
        batch_sizes.append(len(task.tasks))
        results = [success(item.input["nodeId"], item.input["nodeId"]) for item in task.tasks]
        try:
            task = generator.send(results)
        except StopIteration as stopped:
            result = stopped.value
            break

    assert batch_sizes == [2, 2, 1]
    assert [call.input["nodeId"] for call in context.calls] == ["A", "B", "C", "D", "E"]
    assert [call.input["remainingTokenBudget"] for call in context.calls[:2]] == [50, 50]
    assert result["status"] == "succeeded"


def test_failed_dependency_is_blocked_and_never_scheduled() -> None:
    context = FakeContext()
    generator = orchestrate_dag(context, FlowWorkflowInput.model_validate(workflow_input()), join)

    first = next(generator)
    result = finish(generator, [failure(), success("B", {"b": 2})])

    assert [task.input["nodeId"] for task in first.tasks] == ["A", "B"]
    assert [call.input["nodeId"] for call in context.calls] == ["A", "B"]
    assert result["status"] == "failed"
    assert result["nodes"]["A"]["attempt"] == 2
    assert result["nodes"]["C"] == {
        "status": "blocked",
        "blockedBy": ["A"],
    }


@pytest.mark.parametrize("node_result", [success("A", {"ok": True}), failure()])
def test_runtime_caps_wrap_provider_dispatch_and_release_on_every_outcome(
    node_result: dict[str, Any],
) -> None:
    context = FakeContext()
    generator = orchestrate_dag(
        context,
        FlowWorkflowInput.model_validate(
            workflow_input(nodes=[{"id": "A", "instructions": "A", "model": "fake:alpha"}])
        ),
        join,
        persist_caps=True,
    )

    accepted = next(generator)
    reserved = generator.send(cap_result())
    joined = generator.send(cap_result())
    released = generator.send([node_result])
    result = finish(generator, cap_result())

    assert accepted.activity == "ApplyFlowCapTransition"
    assert accepted.input["kind"] == "accept_nodes"
    assert reserved.input["kind"] == "dispatch"
    assert [task.activity for task in joined.tasks] == ["ExecuteFlowNode"]
    assert released.input["kind"] == "complete"
    assert [call.activity for call in context.calls] == [
        "ApplyFlowCapTransition",
        "ApplyFlowCapTransition",
        "ExecuteFlowNode",
        "ApplyFlowCapTransition",
    ]
    assert result["status"] == ("succeeded" if node_result["status"] == "success" else "failed")


def test_runtime_cap_denial_never_invokes_provider() -> None:
    context = FakeContext()
    generator = orchestrate_dag(
        context,
        FlowWorkflowInput.model_validate(
            workflow_input(nodes=[{"id": "A", "instructions": "A", "model": "fake:alpha"}])
        ),
        join,
        persist_caps=True,
    )

    next(generator)
    dispatch = generator.send(cap_result())
    released = generator.send(cap_result(allowed=False))
    result = finish(generator, cap_result())

    assert dispatch.input["kind"] == "dispatch"
    assert released.input["kind"] == "complete"
    assert all(call.activity != "ExecuteFlowNode" for call in context.calls)
    assert result["status"] == "failed"
    assert result["nodes"]["A"]["failure"]["category"] == "budget"


def test_runtime_cap_queue_runs_reserved_work_before_retrying_deferred_node() -> None:
    context = FakeContext()
    generator = orchestrate_dag(
        context,
        FlowWorkflowInput.model_validate(
            workflow_input(
                nodes=[
                    {"id": "A", "instructions": "A", "model": "fake:alpha"},
                    {"id": "B", "instructions": "B", "model": "fake:alpha"},
                ]
            )
        ),
        join,
        persist_caps=True,
    )

    next(generator)
    dispatch_a = generator.send(cap_result())
    dispatch_b = generator.send(cap_result())
    joined_a = generator.send(cap_result(allowed=False, queued=True))
    released_a = generator.send([success("A", {"a": True})])
    retried_b = generator.send(cap_result())
    joined_b = generator.send(cap_result())
    released_b = generator.send([success("B", {"b": True})])
    result = finish(generator, cap_result())

    assert dispatch_a.input["nodeId"] == "A"
    assert dispatch_b.input["idempotencyKey"] == retried_b.input["idempotencyKey"]
    assert dispatch_b.input["requiredTokens"] == 50
    assert retried_b.input["requiredTokens"] == 98
    assert [task.input["nodeId"] for task in joined_a.tasks] == ["A"]
    assert [task.input["nodeId"] for task in joined_b.tasks] == ["B"]
    assert released_a.input["nodeId"] == "A"
    assert released_b.input["nodeId"] == "B"
    assert result["status"] == "succeeded"


def test_runtime_caps_roll_back_partial_batch_when_later_reservation_raises() -> None:
    context = FakeContext()
    generator = orchestrate_dag(
        context,
        FlowWorkflowInput.model_validate(
            workflow_input(
                nodes=[
                    {"id": "A", "instructions": "A", "model": "fake:alpha"},
                    {"id": "B", "instructions": "B", "model": "fake:alpha"},
                ]
            )
        ),
        join,
        persist_caps=True,
    )

    next(generator)
    dispatch_a = generator.send(cap_result())
    dispatch_b = generator.send(cap_result())
    released_a = generator.throw(RuntimeError("cap store unavailable"))
    released_b = generator.send(cap_result())

    assert dispatch_a.input["nodeId"] == "A"
    assert dispatch_b.input["nodeId"] == "B"
    assert released_a.input["kind"] == "complete"
    assert released_a.input["nodeId"] == "A"
    assert released_b.input["kind"] == "complete"
    assert released_b.input["nodeId"] == "B"
    with pytest.raises(RuntimeError, match="cap store unavailable"):
        generator.send(cap_result())


def test_runtime_caps_release_reservation_when_provider_batch_raises() -> None:
    context = FakeContext()
    generator = orchestrate_dag(
        context,
        FlowWorkflowInput.model_validate(
            workflow_input(nodes=[{"id": "A", "instructions": "A", "model": "fake:alpha"}])
        ),
        join,
        persist_caps=True,
    )

    next(generator)
    dispatch = generator.send(cap_result())
    assert dispatch.input["kind"] == "dispatch"
    joined = generator.send(cap_result())
    assert isinstance(joined, Joined)
    released = generator.throw(RuntimeError("provider batch failed"))

    assert released.input["kind"] == "complete"
    with pytest.raises(RuntimeError, match="provider batch failed"):
        generator.send(cap_result())


def test_runtime_caps_release_reservation_before_rejecting_bad_result_count() -> None:
    context = FakeContext()
    generator = orchestrate_dag(
        context,
        FlowWorkflowInput.model_validate(
            workflow_input(nodes=[{"id": "A", "instructions": "A", "model": "fake:alpha"}])
        ),
        join,
        persist_caps=True,
    )

    next(generator)
    dispatch = generator.send(cap_result())
    assert dispatch.input["kind"] == "dispatch"
    joined = generator.send(cap_result())
    assert isinstance(joined, Joined)
    released = generator.send([])

    assert released.input["kind"] == "complete"
    with pytest.raises(RuntimeError, match="unexpected activity result count"):
        generator.send(cap_result())


def test_runtime_caps_retire_dependency_blocked_nodes() -> None:
    context = FakeContext()
    generator = orchestrate_dag(
        context,
        FlowWorkflowInput.model_validate(
            workflow_input(
                nodes=[
                    {"id": "A", "instructions": "A", "model": "fake:alpha"},
                    {
                        "id": "B",
                        "instructions": "B",
                        "dependsOn": ["A"],
                        "model": "fake:alpha",
                    },
                ]
            )
        ),
        join,
        persist_caps=True,
    )

    next(generator)
    dispatch = generator.send(cap_result())
    assert dispatch.input["kind"] == "dispatch"
    joined = generator.send(cap_result())
    assert isinstance(joined, Joined)
    released_a = generator.send([failure()])
    retired_b = generator.send(cap_result())
    result = finish(generator, cap_result())

    assert released_a.input["kind"] == "complete"
    assert retired_b.input["kind"] == "complete"
    assert retired_b.input["nodeId"] == "B"
    provider_node_ids = [
        call.input.get("nodeId") for call in context.calls if call.activity == "ExecuteFlowNode"
    ]
    assert provider_node_ids == ["A"]
    assert result["nodes"]["B"]["status"] == "blocked"


def test_persisted_dependency_results_are_restored_before_dispatch() -> None:
    value = workflow_input()
    value["persistedResults"] = {
        "A": success("A", {"a": 1}),
        "B": success("B", {"b": 2}),
    }
    context = FakeContext()
    generator = orchestrate_dag(context, FlowWorkflowInput.model_validate(value), join)

    task = next(generator)

    assert [call.input["nodeId"] for call in context.calls] == ["C"]
    assert task.tasks[0].input["dependencyOutputs"] == {
        "A": {"a": 1},
        "B": {"b": 2},
    }
    result = finish(generator, [success("C", 3)])
    assert result["status"] == "succeeded"
    assert [event["type"] for event in result["events"][:2]] == [
        "node_restored",
        "node_restored",
    ]


def test_stable_node_identity_is_deterministic_and_attempt_is_separate() -> None:
    context = FakeContext()
    parsed = FlowWorkflowInput.model_validate(workflow_input())

    first = orchestrate_dag(context, parsed, join)
    first_batch = next(first)
    replay = orchestrate_dag(FakeContext(), parsed, join)
    replay_batch = next(replay)

    assert [task.input for task in first_batch.tasks] == [
        task.input for task in replay_batch.tasks
    ]
    assert first_batch.tasks[0].input["nodeExecutionId"] == derive_node_execution_id("run-1", "A")
    assert derive_node_execution_id("run-1", "A") != derive_node_execution_id("run-2", "A")
    assert derive_node_execution_id("run-1", "A") != derive_node_execution_id("run-1", "B")
    assert first_batch.tasks[0].input["attempt"] == 1


def test_custom_status_exposes_ordered_provider_neutral_state_events() -> None:
    context = FakeContext()
    generator = orchestrate_dag(context, FlowWorkflowInput.model_validate(workflow_input()), join)

    next(generator)
    generator.send([success("A", 1), success("B", 2)])

    assert context.custom_statuses[0]["runId"] == "run-1"
    assert context.custom_statuses[0]["status"] == "running"
    assert context.custom_statuses[-1]["nodes"]["A"]["status"] == "succeeded"
    assert [event["sequence"] for event in context.custom_statuses[-1]["events"]] == list(
        range(1, len(context.custom_statuses[-1]["events"]) + 1)
    )
    assert "credential" not in repr(context.custom_statuses)
    assert "providerObject" not in repr(context.custom_statuses)


def test_workflow_name_is_stable() -> None:
    assert FLOW_WORKFLOW_NAME == "FlowDagWorkflow"


def expandable_input(*, max_nodes: int = 2, max_rounds: int = 2) -> dict[str, Any]:
    return workflow_input(
        nodes=[
            {
                "id": "A",
                "instructions": "Expand once",
                "model": "fake:alpha",
                "canExpand": True,
            }
        ]
    ) | {
        "dagSpec": {
            "version": "1.0",
            "nodes": [
                {
                    "id": "A",
                    "instructions": "Expand once",
                    "model": "fake:alpha",
                    "canExpand": True,
                }
            ],
            "caps": {
                "maxNodes": max_nodes,
                "maxRounds": max_rounds,
                "maxConcurrent": 1,
                "tokenBudget": 100,
            },
        }
    }


def expansion_success() -> dict[str, Any]:
    result = success("A", {"seed": 1})
    result["expansionRequest"] = {
        "nodeId": "A",
        "round": 2,
        "nodes": [
            {
                "id": "B",
                "instructions": "Use A",
                "dependsOn": ["A"],
                "model": "fake:alpha",
            }
        ],
    }
    return result


def test_valid_expansion_continues_with_atomic_normalized_state() -> None:
    context = FakeContext()
    generator = orchestrate_dag(
        context,
        FlowWorkflowInput.model_validate(expandable_input()),
        join,
    )

    next(generator)
    continued = finish(generator, [expansion_success()])

    assert continued["status"] == "continued"
    assert len(context.continuations) == 1
    continuation = context.continuations[0]
    assert continuation["currentRound"] == 2
    assert continuation["usedTokens"] == 2
    assert [node["id"] for node in continuation["dagSpec"]["nodes"]] == ["A", "B"]
    assert set(continuation["persistedResults"]) == {"A"}
    assert [event["type"] for event in continuation["persistedEvents"]] == [
        "node_scheduled",
        "usage",
        "node_succeeded",
        "expansion",
    ]

    resumed_context = FakeContext()
    resumed = orchestrate_dag(
        resumed_context,
        FlowWorkflowInput.model_validate(continuation),
        join,
    )
    task = next(resumed)
    assert [item.input["nodeId"] for item in task.tasks] == ["B"]
    completed = finish(resumed, [success("B", {"done": True})])
    assert completed["status"] == "succeeded"
    assert completed["events"][:4] == continuation["persistedEvents"]


def test_audited_expansion_is_checkpointed_before_continued_work_dispatches() -> None:
    first_context = FakeContext()
    first = orchestrate_dag(
        first_context,
        FlowWorkflowInput.model_validate(expandable_input()),
        join,
        persist_audit=True,
    )

    bootstrap = next(first)
    assert bootstrap.activity == "PersistFlowAuditEvents"
    dispatch_audit = first.send({"eventIds": []})
    assert dispatch_audit.activity == "PersistFlowAuditEvents"
    joined = first.send({"eventIds": []})
    assert [task.input["nodeId"] for task in joined.tasks] == ["A"]
    try:
        first.send([expansion_success()])
    except StopIteration as stopped:
        continuation_result = stopped.value
    else:
        raise AssertionError("workflow did not continue")
    assert continuation_result["status"] == "continued"

    continuation = first_context.continuations[0]
    resumed_context = FakeContext()
    resumed = orchestrate_dag(
        resumed_context,
        FlowWorkflowInput.model_validate(continuation),
        join,
        persist_audit=True,
    )
    resumed_bootstrap = next(resumed)
    audited_types = [event["type"] for event in resumed_bootstrap.input["events"]]

    assert "expansion" in audited_types
    assert "node_succeeded" in audited_types
    assert "dependencyOutputs" not in str(resumed_bootstrap.input)
    assert "persistedResults" not in str(resumed_bootstrap.input)
    dispatch_audit = resumed.send({"eventIds": []})
    assert dispatch_audit.activity == "PersistFlowAuditEvents"
    joined = resumed.send({"eventIds": []})
    assert [task.input["nodeId"] for task in joined.tasks] == ["B"]
    assert resumed_context.calls.index(resumed_bootstrap) < resumed_context.calls.index(
        joined.tasks[0]
    )


def test_runtime_caps_release_before_expansion_and_accept_the_next_round() -> None:
    first_context = FakeContext()
    first = orchestrate_dag(
        first_context,
        FlowWorkflowInput.model_validate(expandable_input()),
        join,
        persist_caps=True,
    )

    accepted = next(first)
    reserved = first.send(cap_result())
    joined = first.send(cap_result())
    released = first.send([expansion_success()])
    continued = finish(first, cap_result())

    assert accepted.input["kind"] == "accept_nodes"
    assert accepted.input["roundNumber"] == 1
    assert reserved.input["kind"] == "dispatch"
    assert [task.activity for task in joined.tasks] == ["ExecuteFlowNode"]
    assert released.input["kind"] == "complete"
    assert continued["status"] == "continued"

    resumed_context = FakeContext()
    resumed = orchestrate_dag(
        resumed_context,
        FlowWorkflowInput.model_validate(first_context.continuations[0]),
        join,
        persist_caps=True,
    )
    next_round = next(resumed)
    next_dispatch = resumed.send(cap_result())

    assert next_round.input["kind"] == "accept_nodes"
    assert next_round.input["roundNumber"] == 2
    assert next_round.input["nodeIds"] == ["A", "B"]
    assert next_dispatch.input["kind"] == "dispatch"
    assert next_dispatch.input["nodeId"] == "B"


def test_expansion_replay_produces_identical_single_continuation() -> None:
    value = FlowWorkflowInput.model_validate(expandable_input())
    continuations = []
    for _ in range(2):
        context = FakeContext()
        generator = orchestrate_dag(context, value, join)
        next(generator)
        finish(generator, [expansion_success()])
        assert len(context.continuations) == 1
        continuations.append(context.continuations[0])

    assert continuations[0] == continuations[1]
    assert len(continuations[0]["appliedExpansions"]) == 1


def test_expansion_cap_violation_is_atomic_and_reports_cap_reached() -> None:
    context = FakeContext()
    generator = orchestrate_dag(
        context,
        FlowWorkflowInput.model_validate(expandable_input(max_nodes=1)),
        join,
    )

    next(generator)
    result = finish(generator, [expansion_success()])

    assert context.continuations == []
    assert result["status"] == "rejected"
    assert result["expansionStatus"] == "cap_reached"
    assert set(result["nodes"]) == {"A"}
    denial = next(event for event in result["events"] if event["type"] == "cap_denial")
    assert denial["cap"] == "maxNodes"
    assert denial["currentRound"] == 1
    assert denial["current"] == 1
    assert denial["proposed"] == 2
    assert denial["limit"] == 1
    assert denial["usedTokens"] == 2


@pytest.mark.parametrize(
    ("max_nodes", "expansion_result", "expected_type", "expected_code"),
    [
        (1, expansion_success(), "cap_denial", "max_nodes_exceeded"),
        (
            2,
            {
                **expansion_success(),
                "expansionRequest": {
                    "nodeId": "A",
                    "round": 2,
                    "nodes": [
                        {
                            "id": "B",
                            "instructions": "Use missing dependency",
                            "dependsOn": ["MISSING"],
                            "model": "fake:alpha",
                        }
                    ],
                },
            },
            "expansion_rejected",
            "dangling_dependency",
        ),
    ],
)
def test_audited_expansion_denials_keep_round_and_safe_error_codes(
    max_nodes: int,
    expansion_result: dict[str, Any],
    expected_type: str,
    expected_code: str,
) -> None:
    context = FakeContext()
    generator = orchestrate_dag(
        context,
        FlowWorkflowInput.model_validate(expandable_input(max_nodes=max_nodes)),
        join,
        persist_audit=True,
    )

    assert next(generator).activity == "PersistFlowAuditEvents"
    assert generator.send({"eventIds": []}).activity == "PersistFlowAuditEvents"
    joined = generator.send({"eventIds": []})
    assert [task.input["nodeId"] for task in joined.tasks] == ["A"]
    result_audit = generator.send([expansion_result])
    denial = next(
        event for event in result_audit.input["events"] if event["type"] == expected_type
    )

    assert denial["metadata"]["round"] == 1
    assert expected_code in denial["metadata"]["errorCodes"]
    assert "errors" not in denial["metadata"]
    terminal_audit = generator.send({"eventIds": []})
    assert terminal_audit.activity == "PersistFlowAuditEvents"
    completed = finish(generator, {"eventIds": []})
    assert completed["status"] == "rejected"


def test_max_rounds_terminates_unbounded_expansion() -> None:
    context = FakeContext()
    generator = orchestrate_dag(
        context,
        FlowWorkflowInput.model_validate(expandable_input(max_rounds=1)),
        join,
    )

    next(generator)
    result = finish(generator, [expansion_success()])

    assert result["status"] == "rejected"
    assert result["expansionStatus"] == "cap_reached"
    assert context.continuations == []
    denial = next(event for event in result["events"] if event["type"] == "cap_denial")
    assert denial["cap"] == "maxRounds"
