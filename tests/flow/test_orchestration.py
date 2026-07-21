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

    def call_activity(self, activity: str, *, input: dict[str, Any]) -> FakeTask:
        task = FakeTask(activity, input)
        self.calls.append(task)
        return task

    def set_custom_status(self, value: str) -> None:
        self.custom_statuses.append(json.loads(value))


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
            "provider": "fake",
            "model": "alpha",
            "attempt": 1,
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
    assert first_batch.tasks[0].input["nodeExecutionId"] == derive_node_execution_id(
        "run-1", "A"
    )
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
