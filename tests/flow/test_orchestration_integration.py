from dapr.ext.workflow import WorkflowRuntime

from omnigent.flow.orchestration import (
    FLOW_WORKFLOW_NAME,
    FlowWorkflowInput,
    register_flow_workflow,
)


def test_real_dapr_runtime_registers_typed_flow_workflow() -> None:
    runtime = WorkflowRuntime()

    workflow = register_flow_workflow(runtime)

    assert workflow.__dict__["_workflow_registered"] is True
    assert workflow.__dict__["_dapr_alternate_name"] == FLOW_WORKFLOW_NAME


def test_dapr_model_dump_round_trips_with_public_json_names() -> None:
    value = FlowWorkflowInput.model_validate(
        {
            "runId": "run-1",
            "approvedDagDigest": "sha256:approved",
            "dagSpec": {
                "version": "1.0",
                "nodes": [
                    {"id": "A", "instructions": "Answer", "model": "fake:alpha"}
                ],
                "caps": {
                    "maxNodes": 1,
                    "maxRounds": 1,
                    "maxConcurrent": 1,
                    "tokenBudget": 10,
                },
            },
        }
    )

    dumped = value.model_dump(mode="json")

    assert set(dumped) == {"runId", "approvedDagDigest", "dagSpec", "persistedResults"}
    assert FlowWorkflowInput.model_validate(dumped) == value
