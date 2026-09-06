import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).parents[2]
HARNESS_DIR = REPO / "docs" / "flow" / "harnesses"
EXPECTED_TOOLS = [
    "propose_dag",
    "run_workflow",
    "get_workflow_status",
    "list_workflows",
]


def test_cursor_configuration_is_complete_current_and_secret_free() -> None:
    config_text = (HARNESS_DIR / "cursor.json").read_text(encoding="utf-8")
    config = json.loads(config_text)
    evidence = json.loads((HARNESS_DIR / "cursor-evidence.json").read_text(encoding="utf-8"))
    conformance = json.loads(
        (HARNESS_DIR / "cursor-conformance-evidence.json").read_text(encoding="utf-8")
    )
    guide = (HARNESS_DIR / "cursor.md").read_text(encoding="utf-8")

    assert config == {
        "mcpServers": {
            "flow": {
                "type": "stdio",
                "command": "flow-mcp",
                "args": [],
                "env": {
                    "FLOW_LOG_LEVEL": "INFO",
                    "FLOW_MODE": "${env:FLOW_MODE}",
                    "FLOW_ACTOR": "${env:FLOW_ACTOR}",
                    "FLOW_SIGNING_KEY": "${env:FLOW_SIGNING_KEY}",
                    "FLOW_APPROVAL_DB": "${env:FLOW_APPROVAL_DB}",
                    "FLOW_APPROVAL_TTL_SECONDS": "${env:FLOW_APPROVAL_TTL_SECONDS}",
                    "FLOW_DAPR_HEALTH_TIMEOUT_SECONDS": (
                        "${env:FLOW_DAPR_HEALTH_TIMEOUT_SECONDS}"
                    ),
                    "DAPR_GRPC_PORT": "${env:DAPR_GRPC_PORT}",
                    "DAPR_HTTP_PORT": "${env:DAPR_HTTP_PORT}",
                    "PYTHONNOUSERSITE": "${env:PYTHONNOUSERSITE}",
                },
            }
        }
    }
    assert evidence["harness"] == "Cursor Agent"
    assert evidence["version"] == "agent 2026.06.04-5fd875e"
    assert evidence["verifiedOn"] == "2026-07-21"
    assert evidence["transport"] == "stdio"
    assert evidence["tools"] == EXPECTED_TOOLS
    assert conformance["issue"] == "CLO-184"
    assert conformance["harness"] == "Cursor Agent"
    assert conformance["harnessVersion"] == "2026.06.04-5fd875e"
    assert conformance["tools"] == EXPECTED_TOOLS
    assert set(conformance["scenarios"]) == {
        "tool-discovery",
        "three-node-approved-run",
        "invalid-graphs",
        "status-and-list",
        "bounded-expansion",
        "interruption-recovery",
        "provider-substitution",
    }
    assert conformance["result"] in {"blocked", "passed"}
    if conformance["result"] == "blocked":
        assert conformance["flowCommit"] is None
        assert conformance["artifact"] is None
        assert conformance["runIds"] == {}
        assert conformance["scenarios"]["tool-discovery"]["status"] == "passed"
        assert all(
            scenario["status"] == "blocked"
            for scenario_id, scenario in conformance["scenarios"].items()
            if scenario_id != "tool-discovery"
        )
    else:
        assert len(conformance["flowCommit"]) == 40
        int(conformance["flowCommit"], 16)
        artifact = conformance["artifact"]
        assert artifact["sha256"] == artifact["firstBuildSha256"]
        assert artifact["sha256"] == artifact["secondBuildSha256"]
        assert len(artifact["sha256"]) == 64
        int(artifact["sha256"], 16)
        assert all(
            scenario["status"] == "passed"
            for scenario in conformance["scenarios"].values()
        )
        run_ids = conformance["runIds"]
        assert all(
            isinstance(run_ids[name], str) and run_ids[name]
            for name in (
                "canonical",
                "boundedExpansion",
                "boundedExpansionDenied",
                "interruptionRecovery",
            )
        )
        assert len(run_ids["providerSubstitution"]) == 2
        all_run_ids = {
            run_ids["canonical"],
            run_ids["boundedExpansion"],
            run_ids["boundedExpansionDenied"],
            run_ids["interruptionRecovery"],
            *run_ids["providerSubstitution"],
        }
        assert len(all_run_ids) == 6
    assert len(evidence["officialSources"]) >= 3
    assert ".cursor/mcp.json" in guide
    assert "agent mcp list-tools flow" in guide
    assert "restart Cursor" in guide
    assert "stderr" in guide
    assert "working directory" in guide.lower()
    for marker in ("BEGIN PRIVATE KEY", "sk-live-", "approvalToken"):
        assert marker not in config_text
    for value in config["mcpServers"]["flow"]["env"].values():
        assert value == "INFO" or value.startswith("${env:")


async def test_cursor_configuration_discovers_flow_over_stdio(
    tmp_path: Path, flow_discovery_env: dict[str, str]
) -> None:
    config = json.loads((HARNESS_DIR / "cursor.json").read_text(encoding="utf-8"))
    flow = config["mcpServers"]["flow"]
    parameters = StdioServerParameters(
        command=flow["command"],
        args=flow["args"],
        cwd=tmp_path,
        env={
            **os.environ,
            **flow["env"],
            **flow_discovery_env,
            "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
        },
    )

    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            discovered = await session.list_tools()

    assert [tool.name for tool in discovered.tools] == EXPECTED_TOOLS
