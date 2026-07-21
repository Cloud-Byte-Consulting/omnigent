"""Flow MCP stdio boundary."""

from __future__ import annotations

import logging
import os
import re
import signal
import sys
from collections.abc import Sequence
from typing import Annotated, Any, Literal, Protocol

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ContentBlock
from pydantic import Field, ValidationError

JsonObject = dict[str, object]
NonEmptyString = Annotated[str, Field(min_length=1, pattern=r".*\S.*")]
PageLimit = Annotated[int, Field(ge=1, le=100)]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LOG_LEVELS: tuple[LogLevel, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class FlowService(Protocol):
    async def propose_dag(
        self,
        task_description: str,
        constraints: JsonObject | None = None,
    ) -> JsonObject: ...

    async def run_workflow(
        self,
        dag_spec: JsonObject,
        approval_token: str | None = None,
        confirm: bool = False,
        idempotency_key: str | None = None,
    ) -> JsonObject: ...

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


class _UnavailableFlowService:
    async def propose_dag(
        self,
        _task_description: str,
        _constraints: JsonObject | None = None,
    ) -> JsonObject:
        return _unavailable("propose_dag")

    async def run_workflow(
        self,
        _dag_spec: JsonObject,
        _approval_token: str | None = None,
        _confirm: bool = False,
        _idempotency_key: str | None = None,
    ) -> JsonObject:
        return _unavailable("run_workflow")

    async def get_workflow_status(self, _run_id: str) -> JsonObject:
        return _unavailable("get_workflow_status")

    async def list_workflows(
        self,
        _status: str | None,
        _cursor: str | None,
        _limit: int,
        _created_after: str | None = None,
        _created_before: str | None = None,
        _updated_after: str | None = None,
        _updated_before: str | None = None,
    ) -> JsonObject:
        return _unavailable("list_workflows")


class FlowMCP(FastMCP[None]):
    """FastMCP with one stable invalid-input error code."""

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        try:
            result = await super().call_tool(name, arguments)
        except ToolError as error:
            if isinstance(error.__cause__, ValidationError):
                raise ToolError("invalid_input") from error
            raise
        return result


def create_server(
    service: FlowService | None = None,
    *,
    log_level: str = "INFO",
) -> FlowMCP:
    """Create the deterministic four-tool server around an injected service."""
    normalized_level = log_level.upper()
    if normalized_level not in LOG_LEVELS:
        raise ValueError(f"FLOW_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")
    selected = service or _UnavailableFlowService()
    server = FlowMCP(
        "flow",
        instructions="Propose, run, and inspect provider-neutral Flow workflows.",
        log_level=normalized_level,
    )

    @server.tool()
    async def propose_dag(
        task_description: NonEmptyString,
        constraints: JsonObject | None = None,
    ) -> JsonObject:
        """Propose a provider-neutral DAG for a non-empty task description."""
        return await selected.propose_dag(task_description, constraints)

    @server.tool()
    async def run_workflow(
        dag_spec: JsonObject,
        approval_token: str | None = None,
        confirm: bool = False,
        idempotency_key: str | None = None,
    ) -> JsonObject:
        """Validate, approve, and start a portable Flow DAG specification."""
        return await selected.run_workflow(
            dag_spec,
            approval_token,
            confirm,
            idempotency_key,
        )

    @server.tool()
    async def get_workflow_status(run_id: NonEmptyString) -> JsonObject:
        """Get the current normalized state for a Flow run ID."""
        return await selected.get_workflow_status(run_id)

    @server.tool()
    async def list_workflows(
        status: str | None = None,
        cursor: str | None = None,
        limit: PageLimit = 20,
        created_after: str | None = None,
        created_before: str | None = None,
        updated_after: str | None = None,
        updated_before: str | None = None,
    ) -> JsonObject:
        """List Flow runs with optional status and cursor pagination."""
        return await selected.list_workflows(
            status,
            cursor,
            limit,
            created_after,
            created_before,
            updated_after,
            updated_before,
        )

    return server


def redact(message: str) -> str:
    """Remove common credential forms before writing a diagnostic."""
    patterns = (
        r"(?i)(?:authorization:\s*)?bearer\s+\S+",
        r"(?i)api[_-]?key\s*=\s*\S+",
        r"\bsk-[A-Za-z0-9_-]{8,}\b",
    )
    for pattern in patterns:
        message = re.sub(pattern, "[REDACTED]", message)
    return message


def _unavailable(operation: str) -> JsonObject:
    return {
        "error": {
            "code": "not_implemented",
            "message": f"{operation} service is not connected",
        }
    }


def main() -> int:
    """Run Flow over stdio, keeping diagnostics off stdout."""
    try:
        server = create_server(log_level=os.environ.get("FLOW_LOG_LEVEL", "INFO"))
    except ValueError as error:
        print(redact(str(error)), file=sys.stderr)
        return 2
    logging.basicConfig(stream=sys.stderr)
    anyio.run(_run_stdio, server)
    return 0


async def _run_stdio(server: FlowMCP) -> None:
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(server.run_stdio_async)
        with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
            async for _ in signals:
                tasks.cancel_scope.cancel()
                break


if __name__ == "__main__":
    raise SystemExit(main())
