import json
import os
import sys
from pathlib import Path

import tomllib
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


def test_codex_configuration_is_current_complete_and_secret_free() -> None:
    config_text = (HARNESS_DIR / "codex.toml").read_text(encoding="utf-8")
    config = tomllib.loads(config_text)
    evidence = json.loads((HARNESS_DIR / "codex-evidence.json").read_text(encoding="utf-8"))
    conformance = json.loads(
        (HARNESS_DIR / "codex-conformance-evidence.json").read_text(encoding="utf-8")
    )
    guide = (HARNESS_DIR / "codex.md").read_text(encoding="utf-8")

    flow = config["mcp_servers"]["flow"]
    assert flow == {
        "enabled": True,
        "required": True,
        "command": "flow-mcp",
        "args": [],
        "env": {"FLOW_LOG_LEVEL": "INFO"},
        "startup_timeout_sec": 30.0,
        "tool_timeout_sec": 120.0,
    }
    assert evidence["harness"] == "OpenAI Codex"
    assert evidence["version"] == "codex-cli 0.144.0"
    assert evidence["verifiedOn"] == "2026-07-20"
    assert evidence["transport"] == "stdio"
    assert evidence["tools"] == EXPECTED_TOOLS
    assert evidence["officialSource"].startswith("https://")
    assert conformance["harness"] == "OpenAI Codex"
    assert conformance["issue"] == "CLO-182"
    assert conformance["fixtureRevision"] == "flow-conformance-1.0.0"
    assert conformance["tools"] == EXPECTED_TOOLS
    assert conformance["result"] == "passed"
    assert len(conformance["flowCommit"]) == 40
    assert all(character in "0123456789abcdef" for character in conformance["flowCommit"])
    assert set(conformance["scenarios"]) == {
        "three-node-approved-run",
        "invalid-graphs",
        "status-and-list",
        "bounded-expansion",
        "interruption-recovery",
        "provider-substitution",
    }
    assert all(item["status"] == "passed" for item in conformance["scenarios"].values())
    assert len(conformance["artifact"]["sha256"]) == 64
    assert conformance["artifact"]["firstBuildSha256"] == conformance["artifact"]["sha256"]
    assert conformance["artifact"]["secondBuildSha256"] == conformance["artifact"]["sha256"]
    assert conformance["runIds"]["canonical"]
    assert conformance["runIds"]["boundedExpansionDenied"]
    assert len(conformance["runIds"]["providerSubstitution"]) == 2
    run_ids = [
        conformance["runIds"]["canonical"],
        conformance["runIds"]["boundedExpansion"],
        conformance["runIds"]["boundedExpansionDenied"],
        conformance["runIds"]["interruptionRecovery"],
        *conformance["runIds"]["providerSubstitution"],
    ]
    assert len(run_ids) == len(set(run_ids)) == 6
    assert conformance["canonicalResult"] == {
        "state": "succeeded",
        "nodes": ["A", "B", "C"],
        "dependencyOrder": "A and B succeeded before C dispatch",
        "fanInOutput": {"values": ["A", "B"]},
        "firstStartReused": False,
        "replayReused": True,
    }
    assert conformance["safetyResult"]["invalidGraphsDispatched"] is False
    assert conformance["safetyResult"]["staleApprovalDispatched"] is False
    assert conformance["safetyResult"]["deniedCap"] == "maxNodes"
    assert conformance["safetyResult"]["recoveryEffectCounts"] == {"A": 1, "B": 1, "C": 1}
    assert conformance["redaction"]["rawJsonlPersisted"] is False
    assert conformance["redaction"]["secretsPersisted"] is False
    assert "FLOW_CODEX_E2E=1" in conformance["reproduce"]
    assert "restart codex" in guide.lower()
    assert "codex mcp get flow" in guide
    assert "stderr" in guide
    assert "working directory" in guide.lower()
    for marker in ("BEGIN PRIVATE KEY", "sk-live-", "approval_token ="):
        assert marker not in config_text
        assert marker not in json.dumps(conformance)


async def test_codex_configuration_discovers_flow_over_stdio(
    tmp_path: Path, flow_discovery_env: dict[str, str]
) -> None:
    config = tomllib.loads((HARNESS_DIR / "codex.toml").read_text(encoding="utf-8"))
    flow = config["mcp_servers"]["flow"]
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
