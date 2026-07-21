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
    evidence = json.loads(
        (HARNESS_DIR / "codex-evidence.json").read_text(encoding="utf-8")
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
    assert "restart codex" in guide.lower()
    assert "codex mcp get flow" in guide
    assert "stderr" in guide
    assert "working directory" in guide.lower()
    for marker in ("BEGIN PRIVATE KEY", "sk-live-", "approval_token ="):
        assert marker not in config_text


async def test_codex_configuration_discovers_flow_over_stdio(tmp_path: Path) -> None:
    config = tomllib.loads(
        (HARNESS_DIR / "codex.toml").read_text(encoding="utf-8")
    )
    flow = config["mcp_servers"]["flow"]
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
