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


def test_opencode_configuration_is_complete_current_and_secret_free() -> None:
    config_text = (HARNESS_DIR / "opencode.json").read_text(encoding="utf-8")
    config = json.loads(config_text)
    evidence = json.loads((HARNESS_DIR / "opencode-evidence.json").read_text(encoding="utf-8"))
    guide = (HARNESS_DIR / "opencode.md").read_text(encoding="utf-8")

    assert config == {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            "flow": {
                "type": "local",
                "command": ["flow-mcp"],
                "enabled": True,
                "environment": {"FLOW_LOG_LEVEL": "INFO"},
                "timeout": 30000,
            }
        },
        "permission": {"flow_run_workflow": "ask"},
    }
    assert evidence["harness"] == "OpenCode"
    assert evidence["verifiedVersion"] == "1.2.10"
    assert evidence["latestReviewedVersion"] == "1.18.4"
    assert evidence["verifiedOn"] == "2026-07-20"
    assert evidence["transport"] == "stdio"
    assert evidence["tools"] == EXPECTED_TOOLS
    assert len(evidence["officialSources"]) >= 5
    assert "opencode.json" in guide
    assert "opencode mcp list" in guide
    assert "relaunch OpenCode" in guide
    assert "stderr" in guide
    assert "working directory" in guide.lower()
    for marker in ("BEGIN PRIVATE KEY", "sk-live-", "approvalToken"):
        assert marker not in config_text


async def test_opencode_configuration_discovers_flow_over_stdio(
    tmp_path: Path, flow_discovery_env: dict[str, str]
) -> None:
    config = json.loads((HARNESS_DIR / "opencode.json").read_text(encoding="utf-8"))
    flow = config["mcp"]["flow"]
    command, *args = flow["command"]
    parameters = StdioServerParameters(
        command=command,
        args=args,
        cwd=tmp_path,
        env={
            **os.environ,
            **flow["environment"],
            **flow_discovery_env,
            "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
        },
    )

    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            discovered = await session.list_tools()

    assert [tool.name for tool in discovered.tools] == EXPECTED_TOOLS
