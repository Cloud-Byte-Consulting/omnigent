from dapr.ext.workflow import WorkflowRuntime

from omnigent.flow.activity import (
    NODE_EXECUTION_ACTIVITY_NAME,
    NodeActivityInput,
    NodeExecutionActivity,
    register_node_execution_activity,
)


class UnusedRunner:
    async def execute(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("registration must not execute the provider boundary")


def test_dapr_model_dump_can_be_revalidated_at_the_worker_boundary() -> None:
    value = NodeActivityInput.model_validate(
        {
            "nodeExecutionId": "execution-1",
            "runId": "run-1",
            "nodeId": "A",
            "instructions": "Answer",
            "model": "fake:alpha",
            "defaultModel": None,
            "tools": [],
            "dependsOn": [],
            "dependencyOutputs": {},
            "outputSchema": None,
            "remainingTokenBudget": 10,
            "tokenBudget": 10,
            "attempt": 1,
        }
    )

    restored = NodeActivityInput.model_validate(value.model_dump(mode="json"))

    assert restored == value


def test_real_dapr_runtime_registers_the_typed_activity_boundary() -> None:
    runtime = WorkflowRuntime()

    handler = register_node_execution_activity(
        runtime,
        NodeExecutionActivity(UnusedRunner()),  # type: ignore[arg-type]
    )

    assert handler.__dict__["_activity_registered"] is True
    assert handler.__dict__["_dapr_alternate_name"] == NODE_EXECUTION_ACTIVITY_NAME
