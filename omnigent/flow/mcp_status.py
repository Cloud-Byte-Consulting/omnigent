"""MCP application-service adapter for Flow workflow status."""

from __future__ import annotations

from typing import Any, Protocol

JsonObject = dict[str, object]


class StatusReader(Protocol):
    def get(self, run_id: str, *, actor: str) -> dict[str, Any]: ...


class StatusFlowService:
    """Expose canonical status while leaving unrelated MCP work items disconnected."""

    def __init__(self, status: StatusReader, *, actor: str) -> None:
        if not actor.strip():
            raise ValueError("actor is required")
        self._status = status
        self._actor = actor

    async def get_workflow_status(self, run_id: str) -> JsonObject:
        """Delegate authorization, composition, and redaction to the status service."""
        return self._status.get(run_id, actor=self._actor)

    async def propose_dag(
        self,
        task_description: str,
        constraints: JsonObject | None = None,
    ) -> JsonObject:
        del task_description, constraints
        return _unavailable("propose_dag")

    async def run_workflow(
        self,
        dag_spec: JsonObject,
        approval_token: str | None = None,
        confirm: bool = False,
        idempotency_key: str | None = None,
    ) -> JsonObject:
        del dag_spec, approval_token, confirm, idempotency_key
        return _unavailable("run_workflow")

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
        del (
            status,
            cursor,
            limit,
            created_after,
            created_before,
            updated_after,
            updated_before,
        )
        return _unavailable("list_workflows")


def _unavailable(operation: str) -> JsonObject:
    return {
        "error": {
            "code": "not_implemented",
            "message": f"{operation} service is not connected",
        }
    }
