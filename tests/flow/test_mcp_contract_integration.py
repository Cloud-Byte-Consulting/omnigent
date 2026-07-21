import json
import sys
from pathlib import Path
from tempfile import TemporaryFile
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

FIXTURES = Path(__file__).parent / "fixtures"
SERVER = FIXTURES / "mcp_contract_server.py"
MANIFEST = FIXTURES / "mcp_contract_manifest.json"


def _structured(result: Any) -> dict[str, Any]:
    value = result.structuredContent
    assert isinstance(value, dict)
    return value


async def test_real_mcp_client_exercises_all_four_tool_contracts() -> None:
    parameters = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    with TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        async with stdio_client(parameters, errlog=stderr) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "flow"

                discovered = await session.list_tools()
                assert [tool.name for tool in discovered.tools] == [
                    "propose_dag",
                    "run_workflow",
                    "get_workflow_status",
                    "list_workflows",
                ]
                assert all(tool.inputSchema["type"] == "object" for tool in discovered.tools)

                proposal = _structured(
                    await session.call_tool(
                        "propose_dag",
                        {
                            "task_description": "Research, then summarize",
                            "constraints": {
                                "allowedModels": ["fake:planner"],
                                "allowedTools": ["search"],
                            },
                        },
                    )
                )
                assert proposal["status"] == "proposed"
                dag = proposal["dagSpec"]

                preview = _structured(
                    await session.call_tool(
                        "run_workflow",
                        {
                            "dag_spec": dag,
                            "confirm": False,
                            "idempotency_key": "contract-request-1",
                        },
                    )
                )
                assert preview["status"] == "approval_required"

                stale_dag = json.loads(json.dumps(dag))
                stale_dag["nodes"][0]["instructions"] = "Changed after approval"
                stale = _structured(
                    await session.call_tool(
                        "run_workflow",
                        {
                            "dag_spec": stale_dag,
                            "approval_token": preview["approvalToken"],
                            "confirm": True,
                            "idempotency_key": "contract-request-1",
                        },
                    )
                )
                assert stale["error"]["code"] == "approval_invalid"

                start_arguments = {
                    "dag_spec": dag,
                    "approval_token": preview["approvalToken"],
                    "confirm": True,
                    "idempotency_key": "contract-request-1",
                }
                started = _structured(await session.call_tool("run_workflow", start_arguments))
                replayed = _structured(await session.call_tool("run_workflow", start_arguments))
                assert started["runId"] == replayed["runId"] == "run-1"
                assert (started["reused"], replayed["reused"]) == (False, True)

                status = _structured(
                    await session.call_tool(
                        "get_workflow_status", {"run_id": "run-1"}
                    )
                )
                hidden = _structured(
                    await session.call_tool(
                        "get_workflow_status", {"run_id": "private-run"}
                    )
                )
                assert status["state"] == "queued"
                assert hidden["error"]["code"] == "not_found"

                first_page = _structured(
                    await session.call_tool(
                        "list_workflows", {"status": "queued", "limit": 1}
                    )
                )
                second_page = _structured(
                    await session.call_tool(
                        "list_workflows",
                        {
                            "status": "queued",
                            "limit": 1,
                            "cursor": first_page["nextCursor"],
                        },
                    )
                )
                run_ids = [
                    first_page["workflows"][0]["runId"],
                    second_page["workflows"][0]["runId"],
                ]
                assert run_ids == [
                    "run-1",
                    "run-2",
                ]
                assert first_page["visibleCount"] == second_page["visibleCount"] == 2

                invalid = await session.call_tool(
                    "propose_dag", {"task_description": "   "}
                )
                assert invalid.isError is True
                assert isinstance(invalid.content[0], TextContent)
                assert "invalid_input" in invalid.content[0].text

        stderr.seek(0)
        diagnostics = stderr.read()
        assert "secret" not in diagnostics.lower()


def test_mcp_gherkin_traceability_manifest_names_existing_tests() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    required = {
        "capability_negotiation",
        "tool_discovery_schema",
        "invalid_input",
        "propose",
        "preview",
        "approval",
        "stale_approval",
        "idempotent_start",
        "status",
        "list_filters_pagination",
        "canonical_errors",
        "authorization",
        "redaction",
        "stdout_stderr_separation",
        "shutdown",
    }

    assert set(manifest["scenarios"]) == required
    for test_id in manifest["scenarios"].values():
        path_value, test_name = test_id.split("::", maxsplit=1)
        source = (Path(__file__).parents[2] / path_value).read_text(encoding="utf-8")
        assert f"def {test_name}(" in source
