"""Composable MCP application-service adapter for workflow listing."""

from __future__ import annotations

from typing import Protocol

from omnigent.flow.listing import WorkflowListingService

JsonObject = dict[str, object]


class FallbackFlowService(Protocol):
    async def propose_dag(self, task_description: str) -> JsonObject: ...

    async def run_workflow(
        self,
        dag_spec: JsonObject,
        approval_token: str | None = None,
        confirm: bool = False,
        idempotency_key: str | None = None,
    ) -> JsonObject: ...

    async def get_workflow_status(self, run_id: str) -> JsonObject: ...


class ListingFlowService:
    """Add list_workflows to an existing partial Flow application service."""

    def __init__(
        self,
        listing: WorkflowListingService,
        *,
        actor: str,
        fallback: FallbackFlowService,
    ) -> None:
        if not actor.strip():
            raise ValueError("actor is required")
        self._listing = listing
        self._actor = actor
        self._fallback = fallback

    async def list_workflows(
        self,
        status: str | None,
        cursor: str | None,
        limit: int,
        created_after: str | None = None,
        created_before: str | None = None,
        updated_after: str | None = None,
        updated_before: str | None = None,
    ) -> JsonObject:
        return self._listing.list(
            actor=self._actor,
            state=status,
            created_after=created_after,
            created_before=created_before,
            updated_after=updated_after,
            updated_before=updated_before,
            cursor=cursor,
            limit=limit,
        )

    async def propose_dag(self, task_description: str) -> JsonObject:
        return await self._fallback.propose_dag(task_description)

    async def run_workflow(
        self,
        dag_spec: JsonObject,
        approval_token: str | None = None,
        confirm: bool = False,
        idempotency_key: str | None = None,
    ) -> JsonObject:
        return await self._fallback.run_workflow(
            dag_spec,
            approval_token,
            confirm,
            idempotency_key,
        )

    async def get_workflow_status(self, run_id: str) -> JsonObject:
        return await self._fallback.get_workflow_status(run_id)
