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


def test_antigravity_configuration_is_complete_current_and_secret_free() -> None:
    config_text = (HARNESS_DIR / "antigravity.json").read_text(encoding="utf-8")
    config = json.loads(config_text)
    evidence = json.loads(
        (HARNESS_DIR / "antigravity-evidence.json").read_text(encoding="utf-8")
    )
    guide = (HARNESS_DIR / "antigravity.md").read_text(encoding="utf-8")

    assert config == {
        "mcpServers": {
            "flow": {
                "command": "flow-mcp",
                "args": [],
                "env": {"FLOW_LOG_LEVEL": "INFO"},
            }
        }
    }
    assert evidence["harness"] == "Google Antigravity"
    assert evidence["version"] == "Antigravity IDE 2.1.1"
    assert evidence["verifiedOn"] == "2026-07-20"
    assert evidence["transport"] == "stdio"
    assert evidence["tools"] == EXPECTED_TOOLS
    assert len(evidence["officialSources"]) >= 3
    assert all(source.startswith("https://") for source in evidence["officialSources"])
    assert "~/.gemini/config/mcp_config.json" in guide
    assert "Refresh" in guide
    assert "stderr" in guide
    assert "working directory" in guide.lower()
    for marker in ("BEGIN PRIVATE KEY", "sk-live-", "approvalToken"):
        assert marker not in config_text


async def test_antigravity_configuration_discovers_flow_over_stdio(
    tmp_path: Path,
) -> None:
    config = json.loads(
        (HARNESS_DIR / "antigravity.json").read_text(encoding="utf-8")
    )
    flow = config["mcpServers"]["flow"]
    parameters = StdioServerParameters(
        command=flow["command"],
        args=flow["args"],
        cwd=tmp_path,
        env={
            **os.environ,
            **flow["env"],
            "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
        },
    )

    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            discovered = await session.list_tools()

    assert [tool.name for tool in discovered.tools] == EXPECTED_TOOLS
