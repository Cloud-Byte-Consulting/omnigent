import json
from dataclasses import dataclass
from typing import Any

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
    }


def join(tasks: list[FakeTask]) -> Joined:
    return Joined(tasks)


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
    assert completed["events"][:3] == continuation["persistedEvents"]


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
