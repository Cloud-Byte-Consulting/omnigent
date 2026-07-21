import pytest
from mcp.server.fastmcp.exceptions import ToolError

from omnigent.flow.mcp_server import create_server, redact


class FakeFlowService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def propose_dag(
        self,
        task_description: str,
        constraints: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self._record(
            "propose_dag",
            {"task_description": task_description, "constraints": constraints},
        )

    async def run_workflow(
        self,
        dag_spec: dict[str, object],
        approval_token: str | None = None,
        confirm: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        return self._record(
            "run_workflow",
            {
                "dag_spec": dag_spec,
                "approval_token": approval_token,
                "confirm": confirm,
                "idempotency_key": idempotency_key,
            },
        )

    async def get_workflow_status(self, run_id: str) -> dict[str, object]:
        return self._record("get_workflow_status", {"run_id": run_id})

    async def list_workflows(
        self,
        status: str | None,
        cursor: str | None,
        limit: int,
        created_after: str | None = None,
        created_before: str | None = None,
        updated_after: str | None = None,
        updated_before: str | None = None,
    ) -> dict[str, object]:
        return self._record(
            "list_workflows",
            {
                "status": status,
                "cursor": cursor,
                "limit": limit,
                "created_after": created_after,
                "created_before": created_before,
                "updated_after": updated_after,
                "updated_before": updated_before,
            },
        )

    def _record(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, arguments))
        return {"operation": name}


async def test_discovers_exactly_four_canonical_tools_with_schemas() -> None:
    server = create_server(FakeFlowService())

    tools = await server.list_tools()

    assert [tool.name for tool in tools] == [
        "propose_dag",
        "run_workflow",
        "get_workflow_status",
        "list_workflows",
    ]
    assert all(tool.description for tool in tools)
    assert all(tool.inputSchema["type"] == "object" for tool in tools)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("propose_dag", {"task_description": "Research and summarize"}),
        ("run_workflow", {"dag_spec": {"version": "1.0"}}),
        ("get_workflow_status", {"run_id": "run-1"}),
        ("list_workflows", {}),
    ],
)
async def test_routes_valid_calls_to_injected_service(
    name: str,
    arguments: dict[str, object],
) -> None:
    service = FakeFlowService()
    server = create_server(service)

    await server.call_tool(name, arguments)

    assert service.calls[-1][0] == name


async def test_invalid_input_does_not_call_service() -> None:
    service = FakeFlowService()
    server = create_server(service)

    with pytest.raises(ToolError, match="invalid_input"):
        await server.call_tool("propose_dag", {})

    assert service.calls == []


def test_redacts_credentials_from_diagnostics() -> None:
    diagnostic = redact("Authorization: Bearer secret-token api_key=secret-value sk-abcdefghijk")

    assert "secret" not in diagnostic
    assert diagnostic.count("[REDACTED]") == 3
