import pytest
from mcp.server.fastmcp.exceptions import ToolError

from omnigent.flow.mcp_server import create_server
from omnigent.flow.mcp_status import StatusFlowService


class RecordingStatusService:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def get(self, run_id: str, *, actor: str) -> dict[str, object]:
        self.calls.append((run_id, actor))
        return self.result


async def test_status_tool_returns_complete_active_or_terminal_view_unchanged() -> None:
    expected = {
        "runId": "run-1",
        "dag": {"digest": "digest", "version": "1.0"},
        "state": "succeeded",
        "timestamps": {"createdAt": "2026-07-21T00:00:00+00:00"},
        "approval": {"approved": True},
        "caps": {"limits": {}, "utilization": {"usedTokens": 10}},
        "nodes": {
            "A": {
                "state": "succeeded",
                "attempts": 1,
                "validatedResultAvailable": True,
            }
        },
        "history": [{"sequence": 1, "type": "node_succeeded"}],
        "expansionHistory": [],
        "interventionReason": None,
        "redaction": {"credentialsExcluded": True},
    }
    status = RecordingStatusService(expected)
    service = StatusFlowService(status, actor="local-operator")

    result = await service.get_workflow_status("run-1")

    assert result == expected
    assert status.calls == [("run-1", "local-operator")]


@pytest.mark.parametrize("actor", ["unauthorized", "operator"])
async def test_status_tool_preserves_not_found_without_existence_leak(actor: str) -> None:
    status = RecordingStatusService({"runId": "missing", "error": "not_found"})

    result = await StatusFlowService(status, actor=actor).get_workflow_status("missing")

    assert result == {"runId": "missing", "error": "not_found"}
    assert set(result) == {"runId", "error"}


async def test_mcp_rejects_blank_run_id_before_status_lookup() -> None:
    status = RecordingStatusService({"runId": "unused"})
    server = create_server(StatusFlowService(status, actor="operator"))

    with pytest.raises(ToolError, match="invalid_input"):
        await server.call_tool("get_workflow_status", {"run_id": "   "})

    assert status.calls == []


async def test_fastmcp_boundary_serializes_canonical_status_as_structured_content() -> None:
    expected = {
        "runId": "run-1",
        "state": "running",
        "nodes": {
            "A": {"state": "blocked"},
            "B": {"state": "queued"},
            "C": {"state": "running"},
            "D": {"state": "succeeded"},
            "E": {"state": "failed"},
        },
        "history": [{"sequence": 1, "type": "run_running"}],
        "redaction": {"credentialsExcluded": True},
    }
    status = RecordingStatusService(expected)
    server = create_server(StatusFlowService(status, actor="operator"))

    content, structured = await server.call_tool(
        "get_workflow_status",
        {"run_id": "run-1"},
    )

    assert structured == expected
    assert '"credentialsExcluded": true' in content[0].text
    assert status.calls == [("run-1", "operator")]


async def test_status_service_does_not_implement_other_tool_work_items() -> None:
    service = StatusFlowService(RecordingStatusService({}), actor="operator")

    assert await service.propose_dag("task") == {
        "error": {
            "code": "not_implemented",
            "message": "propose_dag service is not connected",
        }
    }
