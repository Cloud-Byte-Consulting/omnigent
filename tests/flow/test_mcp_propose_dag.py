from typing import Any

import pytest

from omnigent.flow.mcp_proposal import (
    ClarificationNeed,
    DagProposalService,
    ProposalDraft,
    ProposalGenerationError,
    ProposalRequest,
)
from omnigent.flow.mcp_server import create_server
from omnigent.flow.mcp_status import StatusFlowService


def dag(**changes: Any) -> dict[str, Any]:
    value = {
        "version": "1.0",
        "defaultModel": "fake:planner",
        "nodes": [{"id": "A", "instructions": "Complete the requested task"}],
        "caps": {
            "maxNodes": 1,
            "maxRounds": 1,
            "maxConcurrent": 1,
            "tokenBudget": 100,
        },
    }
    value.update(changes)
    return value


class RecordingGenerator:
    def __init__(self, draft: ProposalDraft) -> None:
        self.draft = draft
        self.requests: list[ProposalRequest] = []

    async def propose(self, request: ProposalRequest) -> ProposalDraft:
        self.requests.append(request)
        return self.draft


class FailingGenerator:
    async def propose(self, request: ProposalRequest) -> ProposalDraft:
        del request
        raise ProposalGenerationError("provider rejected secret-token")


class EmptyStatus:
    def get(self, run_id: str, *, actor: str) -> dict[str, object]:
        return {"runId": run_id, "actor": actor}


def service(draft: ProposalDraft) -> tuple[DagProposalService, RecordingGenerator]:
    generator = RecordingGenerator(draft)
    fallback = StatusFlowService(EmptyStatus(), actor="operator")
    return DagProposalService(generator, fallback=fallback), generator


async def test_proposes_smallest_canonical_valid_dag_without_run_identifiers() -> None:
    proposal, generator = service(
        ProposalDraft(
            dag_spec=dag(),
            assumptions=("The task can be completed by one node.",),
            warnings=(),
        )
    )

    result = await proposal.propose_dag("Summarize the supplied notes")

    assert result["status"] == "proposed"
    assert result["validation"] == {"valid": True, "errors": []}
    assert result["dagSpec"]["nodes"][0]["dependsOn"] == []
    assert result["assumptions"] == ["The task can be completed by one node."]
    assert "runId" not in result
    assert "approvalToken" not in result
    assert generator.requests[0].task_description == "Summarize the supplied notes"


async def test_respects_allowed_models_tools_and_cap_ceilings() -> None:
    constrained_dag = dag(
        nodes=[
            {
                "id": "research",
                "instructions": "Research with the approved search tool",
                "model": "fake:researcher",
                "tools": ["search"],
                "outputSchema": {
                    "type": "object",
                    "properties": {"facts": {"type": "array"}},
                    "required": ["facts"],
                },
            },
            {
                "id": "write",
                "instructions": "Write from the structured research result",
                "dependsOn": ["research"],
                "model": "fake:writer",
            },
        ],
        caps={
            "maxNodes": 2,
            "maxRounds": 1,
            "maxConcurrent": 1,
            "tokenBudget": 200,
        },
    )
    proposal, generator = service(ProposalDraft(dag_spec=constrained_dag))
    constraints = {
        "allowedModels": ["fake:researcher", "fake:writer"],
        "allowedTools": ["search"],
        "caps": {
            "maxNodes": 2,
            "maxRounds": 1,
            "maxConcurrent": 1,
            "tokenBudget": 200,
        },
    }

    result = await proposal.propose_dag("Research facts, then write", constraints)

    assert result["status"] == "proposed"
    assert result["dagSpec"]["nodes"][0]["outputSchema"]["type"] == "object"
    assert result["dagSpec"]["nodes"][1]["dependsOn"] == ["research"]
    assert generator.requests[0].allowed_models == (
        "fake:researcher",
        "fake:writer",
    )
    assert generator.requests[0].allowed_tools == ("search",)


async def test_constraint_violation_rejects_model_output_without_side_effects() -> None:
    proposal, _generator = service(
        ProposalDraft(
            dag_spec=dag(
                defaultModel="forbidden:model",
                nodes=[
                    {
                        "id": "A",
                        "instructions": "Use an unapproved tool",
                        "tools": ["shell"],
                    }
                ],
            )
        )
    )

    result = await proposal.propose_dag(
        "Safe task",
        {
            "allowedModels": ["fake:planner"],
            "allowedTools": ["search"],
            "caps": dag()["caps"],
        },
    )

    assert result["error"]["code"] == "proposal_constraint_violation"
    assert {error["code"] for error in result["validation"]["errors"]} == {
        "model_not_allowed",
        "tool_not_allowed",
    }
    assert "runId" not in result


async def test_invalid_generated_dag_returns_canonical_validation_errors() -> None:
    proposal, _generator = service(
        ProposalDraft(dag_spec=dag(nodes=[{"id": "A", "instructions": "Run"}], caps={}))
    )

    result = await proposal.propose_dag("Safe task")

    assert result["error"]["code"] == "invalid_proposal"
    assert result["validation"]["valid"] is False
    assert result["validation"]["errors"]


async def test_generator_failure_is_canonical_and_does_not_expose_details() -> None:
    fallback = StatusFlowService(EmptyStatus(), actor="operator")
    proposal = DagProposalService(FailingGenerator(), fallback=fallback)

    result = await proposal.propose_dag("Safe task")

    assert result == {
        "error": {
            "code": "proposal_failed",
            "message": "proposal generation failed safely",
        }
    }
    assert "secret-token" not in repr(result)


async def test_returns_structured_clarification_without_inventing_values() -> None:
    need = ClarificationNeed(
        field="destination",
        question="Which approved destination should receive the report?",
        reason="The request asks for external delivery without a destination.",
    )
    proposal, _generator = service(
        ProposalDraft(
            clarification_needs=(need,),
            assumptions=(),
            warnings=("No destination was inferred.",),
        )
    )

    result = await proposal.propose_dag("Send the report")

    assert result == {
        "status": "clarification_required",
        "clarificationNeeds": [
            {
                "field": "destination",
                "question": "Which approved destination should receive the report?",
                "reason": "The request asks for external delivery without a destination.",
            }
        ],
        "assumptions": [],
        "warnings": ["No destination was inferred."],
    }


@pytest.mark.parametrize(
    "task,constraints",
    [
        ("   ", None),
        ("Task", {"surprise": True}),
        ("Task", {"allowedModels": []}),
    ],
)
async def test_rejects_invalid_request_before_generator_call(
    task: str,
    constraints: dict[str, object] | None,
) -> None:
    proposal, generator = service(ProposalDraft(dag_spec=dag()))

    result = await proposal.propose_dag(task, constraints)

    assert result["error"]["code"] == "invalid_input"
    assert generator.requests == []


async def test_fastmcp_constraints_schema_and_composable_status_boundary() -> None:
    proposal, _generator = service(ProposalDraft(dag_spec=dag()))
    server = create_server(proposal)

    _content, result = await server.call_tool(
        "propose_dag",
        {
            "task_description": "Summarize",
            "constraints": {"allowedModels": ["fake:planner"]},
        },
    )
    _status_content, status = await server.call_tool(
        "get_workflow_status", {"run_id": "run-else"}
    )

    assert result["status"] == "proposed"
    assert status == {"runId": "run-else", "actor": "operator"}
