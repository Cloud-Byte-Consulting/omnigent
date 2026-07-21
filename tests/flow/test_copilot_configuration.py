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


def test_copilot_configuration_is_complete_current_and_secret_free() -> None:
    config_text = (REPO / ".mcp.json").read_text(encoding="utf-8")
    config = json.loads(config_text)
    evidence = json.loads(
        (HARNESS_DIR / "copilot-evidence.json").read_text(encoding="utf-8")
    )
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
    assert evidence["verifiedVersion"] == "1.0.56"
    assert evidence["latestReviewedVersion"] == "1.0.73"
    assert evidence["verifiedOn"] == "2026-07-20"
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


async def test_copilot_configuration_discovers_flow_over_stdio(tmp_path: Path) -> None:
    config = json.loads((REPO / ".mcp.json").read_text(encoding="utf-8"))
    flow = config["mcpServers"]["flow"]
    parameters = StdioServerParameters(
        command=flow["command"],
        args=flow["args"],
        cwd=tmp_path,
        env={
            **os.environ,
            "FLOW_LOG_LEVEL": "INFO",
            "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
        },
    )

    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            discovered = await session.list_tools()

    assert [tool.name for tool in discovered.tools] == EXPECTED_TOOLS
