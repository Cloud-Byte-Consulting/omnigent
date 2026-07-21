"""Deterministic full Flow service used by the real MCP stdio contract test."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import chain, count
from typing import Any

from omnigent.flow.approval import ApprovalService, InMemoryApprovalStore
from omnigent.flow.listing import (
    InMemoryWorkflowCatalog,
    WorkflowListingService,
    WorkflowSummary,
)
from omnigent.flow.mcp_listing import ListingFlowService
from omnigent.flow.mcp_proposal import DagProposalService, ProposalDraft, ProposalRequest
from omnigent.flow.mcp_run import ApprovedDaprWorkflowStarter, WorkflowRunFlowService
from omnigent.flow.mcp_server import create_server
from omnigent.flow.mcp_status import StatusFlowService

NOW = datetime(2026, 7, 21, tzinfo=UTC)


class DeterministicProposalGenerator:
    async def propose(self, request: ProposalRequest) -> ProposalDraft:
        return ProposalDraft(
            dag_spec={
                "version": "1.0",
                "defaultModel": "fake:planner",
                "nodes": [
                    {
                        "id": "research",
                        "instructions": f"Research for: {request.task_description}",
                        "tools": ["search"],
                        "outputSchema": {
                            "type": "object",
                            "properties": {"facts": {"type": "array"}},
                            "required": ["facts"],
                        },
                    },
                    {
                        "id": "summarize",
                        "instructions": "Summarize the validated research facts",
                        "dependsOn": ["research"],
                    },
                ],
                "caps": {
                    "maxNodes": 2,
                    "maxRounds": 1,
                    "maxConcurrent": 1,
                    "tokenBudget": 200,
                },
            },
            assumptions=("The approved search tool can access the needed sources.",),
        )


class RecordingWorkflowClient:
    def __init__(self) -> None:
        self.instance_ids: list[str] = []

    def schedule_new_workflow(
        self,
        workflow: str,
        *,
        input: dict[str, Any],
        instance_id: str,
    ) -> None:
        del workflow, input
        self.instance_ids.append(instance_id)


class DeterministicStatus:
    def get(self, run_id: str, *, actor: str) -> dict[str, object]:
        if actor != "contract-operator" or run_id == "private-run":
            return {
                "error": {
                    "code": "not_found",
                    "message": "workflow was not found",
                }
            }
        return {
            "runId": run_id,
            "state": "queued",
            "redaction": {"credentialsExcluded": True},
        }


def _counts(*, queued: int) -> dict[str, int]:
    return {
        "total": queued,
        "blocked": 0,
        "queued": queued,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
        "canceled": 0,
        "skipped": 0,
    }


def _service() -> DagProposalService:
    status = StatusFlowService(DeterministicStatus(), actor="contract-operator")
    catalog = InMemoryWorkflowCatalog(
        [
            WorkflowSummary(
                run_id="run-1",
                dag_digest="digest-1",
                dag_name="First",
                state="queued",
                created_at=NOW,
                updated_at=NOW,
                completed_at=None,
                node_counts=_counts(queued=1),
            ),
            WorkflowSummary(
                run_id="run-2",
                dag_digest="digest-2",
                dag_name="Second",
                state="queued",
                created_at=NOW + timedelta(seconds=1),
                updated_at=NOW + timedelta(seconds=1),
                completed_at=None,
                node_counts=_counts(queued=1),
            ),
            WorkflowSummary(
                run_id="private-run",
                dag_digest="private-digest",
                dag_name=None,
                state="queued",
                created_at=NOW + timedelta(seconds=2),
                updated_at=NOW + timedelta(seconds=2),
                completed_at=None,
                node_counts=_counts(queued=1),
            ),
        ]
    )
    listing = ListingFlowService(
        WorkflowListingService(
            catalog,
            authorizer=lambda actor, run_id: (
                actor == "contract-operator" and run_id != "private-run"
            ),
        ),
        actor="contract-operator",
        fallback=status,
    )
    workflow_client = RecordingWorkflowClient()
    identifiers = chain(("approval-1", "run-1"), (f"unused-{value}" for value in count(1)))
    approvals = ApprovalService(
        InMemoryApprovalStore(),
        signing_key=b"contract-suite-signing-key",
        start_run=ApprovedDaprWorkflowStarter(workflow_client),
        id_factory=lambda: next(identifiers),
    )
    run = WorkflowRunFlowService(
        approvals,
        actor="contract-operator",
        clock=lambda: NOW,
        approval_ttl=timedelta(minutes=5),
        fallback=listing,
    )
    return DagProposalService(DeterministicProposalGenerator(), fallback=run)


if __name__ == "__main__":
    create_server(_service()).run(transport="stdio")
