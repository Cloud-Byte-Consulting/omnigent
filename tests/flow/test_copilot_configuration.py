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
EXPECTED_SCENARIOS = {
    "three-node-approved-run",
    "invalid-graphs",
    "status-and-list",
    "bounded-expansion",
    "interruption-recovery",
    "provider-substitution",
}


def test_copilot_configuration_is_complete_current_and_secret_free() -> None:
    config_text = (REPO / ".mcp.json").read_text(encoding="utf-8")
    config = json.loads(config_text)
    evidence = json.loads((HARNESS_DIR / "copilot-evidence.json").read_text(encoding="utf-8"))
    guide = (HARNESS_DIR / "copilot.md").read_text(encoding="utf-8")

    assert config == {
        "mcpServers": {
            "flow": {
                "type": "stdio",
                "command": "flow-mcp",
                "args": [],
                "env": {"FLOW_LOG_LEVEL": "${FLOW_LOG_LEVEL:-INFO}"},
                "tools": EXPECTED_TOOLS,
                "timeout": 120000,
            }
        }
    }
    assert evidence["harness"] == "GitHub Copilot CLI"
    assert evidence["verifiedVersion"] == "1.0.73"
    assert evidence["latestReviewedVersion"] == "1.0.73"
    assert evidence["verifiedOn"] == "2026-07-21"
    assert evidence["transport"] == "stdio"
    assert evidence["tools"] == EXPECTED_TOOLS
    assert len(evidence["officialSources"]) >= 5
    assert "copilot mcp list --json" in guide
    assert "copilot mcp get flow --json" in guide
    assert "new `copilot` session" in guide
    assert "stderr" in guide
    assert "working directory" in guide.lower()
    for marker in ("BEGIN PRIVATE KEY", "sk-live-", "approvalToken"):
        assert marker not in config_text


def test_copilot_native_conformance_evidence_is_complete_and_redacted() -> None:
    evidence_path = HARNESS_DIR / "copilot-conformance-evidence.json"
    evidence_text = evidence_path.read_text(encoding="utf-8")
    evidence = json.loads(evidence_text)

    assert evidence["schemaVersion"] == "1.0"
    assert evidence["issue"] == "CLO-181"
    assert evidence["harness"] == "GitHub Copilot CLI"
    assert evidence["verifiedVersion"]
    assert evidence["verifiedOn"]
    assert evidence["flowBuild"]["version"]
    assert evidence["flowBuild"]["baseCommit"] == ("3d8a401fe95aac72efd9b00871f14b62251f42f1")
    assert len(evidence["flowBuild"]["wheelSha256"]) == 64
    int(evidence["flowBuild"]["wheelSha256"], 16)
    assert evidence["fixtureRevision"] == "flow-conformance-1.0.0"
    assert evidence["configuration"] == {
        "path": ".mcp.json",
        "transport": "stdio",
        "secrets": (
            "required runtime values are injected into an allowlisted environment "
            "and excluded from evidence"
        ),
    }
    assert evidence["tools"] == EXPECTED_TOOLS
    assert set(evidence["scenarios"]) == EXPECTED_SCENARIOS
    assert all(item["status"] == "passed" for item in evidence["scenarios"].values())
    assert evidence["runIds"]
    assert len(set(evidence["runIds"])) == 6
    assert evidence["scenarios"]["bounded-expansion"]["deniedRunId"] in evidence["runIds"]
    assert set(evidence["scenarios"]["provider-substitution"]["runIds"]) < set(evidence["runIds"])
    assert evidence["canonicalRun"]["state"] == "succeeded"
    assert evidence["canonicalRun"]["nodeStates"] == {
        "A": "succeeded",
        "B": "succeeded",
        "C": "succeeded",
    }
    assert evidence["canonicalRun"]["dependencyOrder"] == ["A", "B", "C"]
    assert evidence["canonicalRun"]["reused"] is True
    assert evidence["knownLimitations"]
    assert evidence["reproduce"] == (
        "FLOW_COPILOT_E2E=1 uv run pytest -q -vv -s tests/flow/test_copilot_conformance_e2e.py"
    )
    for marker in (
        "approvalToken",
        "approval_token",
        "BEGIN PRIVATE KEY",
        "sk-live-",
        "temporary-local-copilot-e2e-key",
    ):
        assert marker not in evidence_text


async def test_copilot_configuration_discovers_flow_over_stdio(
    tmp_path: Path, flow_discovery_env: dict[str, str]
) -> None:
    config = json.loads((REPO / ".mcp.json").read_text(encoding="utf-8"))
    flow = config["mcpServers"]["flow"]
    parameters = StdioServerParameters(
        command=flow["command"],
        args=flow["args"],
        cwd=tmp_path,
        env={
            **os.environ,
            "FLOW_LOG_LEVEL": "INFO",
            **flow_discovery_env,
            "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
        },
    )

    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            discovered = await session.list_tools()

    assert [tool.name for tool in discovered.tools] == EXPECTED_TOOLS
