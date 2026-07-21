"""Two-phase MCP preview, approval, and durable workflow start boundary."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Protocol

from omnigent.flow.approval import ApprovalPreview, ApprovalRecord, ApprovalService
from omnigent.flow.orchestration import FLOW_WORKFLOW_NAME

JsonObject = dict[str, object]


class WorkflowClient(Protocol):
    def schedule_new_workflow(
        self,
        workflow: str,
        *,
        input: dict[str, Any],
        instance_id: str,
    ) -> object: ...


class FallbackFlowService(Protocol):
    async def propose_dag(self, task_description: str) -> JsonObject: ...

    async def get_workflow_status(self, run_id: str) -> JsonObject: ...

    async def list_workflows(
        self,
        status: str | None,
        cursor: str | None,
        limit: int,
        created_after: str | None = None,
        created_before: str | None = None,
        updated_after: str | None = None,
        updated_before: str | None = None,
    ) -> JsonObject: ...


class ApprovedDaprWorkflowStarter:
    """Resolve the exact stored approval snapshot and schedule it once."""

    def __init__(self, client: WorkflowClient) -> None:
        self._client = client

    def __call__(self, run_id: str, record: ApprovalRecord) -> None:
        self._client.schedule_new_workflow(
            FLOW_WORKFLOW_NAME,
            input={
                "runId": run_id,
                "approvedDagDigest": record.dag_digest,
                "dagSpec": record.dag_snapshot,
                "persistedResults": {},
            },
            instance_id=run_id,
        )


class WorkflowRunFlowService:
    """Add run_workflow's two-phase behavior to a partial Flow service."""

    def __init__(
        self,
        approvals: ApprovalService,
        *,
        actor: str,
        clock: Callable[[], datetime],
        approval_ttl: timedelta,
        fallback: FallbackFlowService,
    ) -> None:
        if not actor.strip():
            raise ValueError("actor is required")
        if approval_ttl <= timedelta(0):
            raise ValueError("approval_ttl must be positive")
        self._approvals = approvals
        self._actor = actor
        self._clock = clock
        self._approval_ttl = approval_ttl
        self._fallback = fallback

    async def run_workflow(
        self,
        dag_spec: JsonObject,
        approval_token: str | None = None,
        confirm: bool = False,
        idempotency_key: str | None = None,
    ) -> JsonObject:
        now = self._clock()
        if idempotency_key is not None and not idempotency_key.strip():
            return _error("invalid_input", "idempotency_key cannot be blank")
        if not confirm:
            try:
                preview = self._approvals.preview(dag_spec)
                expiry = now + self._approval_ttl
                token = self._approvals.record_decision(
                    preview,
                    approver=self._actor,
                    decision="approved",
                    decided_at=now,
                    expires_at=expiry,
                    idempotency_key=idempotency_key,
                )
            except ValueError as error:
                return _error("invalid_input", str(error))
            return {
                "status": "approval_required",
                "preview": _preview(preview),
                "dagDigest": preview.digest,
                "approvalToken": token,
                "approvalExpiresAt": expiry.isoformat(),
            }

        if approval_token is None or not approval_token.strip():
            return _error("missing_approval", "approval_token is required when confirm is true")
        result = self._approvals.confirm(
            approval_token,
            dag_spec,
            now=now,
            idempotency_key=idempotency_key,
        )
        if result.error is not None or result.run_id is None:
            return _error("approval_invalid", "approval is invalid, stale, denied, or expired")
        preview = self._approvals.preview(dag_spec)
        return {
            "runId": result.run_id,
            "state": "queued",
            "dagDigest": preview.digest,
            "createdAt": now.isoformat(),
            "updatedAt": now.isoformat(),
            "reused": result.reused,
        }

    async def propose_dag(self, task_description: str) -> JsonObject:
        return await self._fallback.propose_dag(task_description)

    async def get_workflow_status(self, run_id: str) -> JsonObject:
        return await self._fallback.get_workflow_status(run_id)

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
        return await self._fallback.list_workflows(
            status,
            cursor,
            limit,
            created_after,
            created_before,
            updated_after,
            updated_before,
        )


def _preview(preview: ApprovalPreview) -> JsonObject:
    return {
        "digest": preview.digest,
        "contractVersion": preview.contract_version,
        "dag": preview.dag,
        "caps": preview.caps_snapshot,
        "modelsAndTools": [
            {"nodeId": node_id, "model": model, "tools": list(tools)}
            for node_id, model, tools in preview.model_tool_snapshot
        ],
        "validationWarnings": list(preview.validation_warnings),
        "usageEstimate": preview.usage_estimate,
    }


def _error(code: str, message: str) -> JsonObject:
    return {"error": {"code": code, "message": message}}
