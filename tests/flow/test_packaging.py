import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).parents[2]


def _build(output: Path) -> Path:
    env = {**os.environ, "SOURCE_DATE_EPOCH": "1767225600"}
    subprocess.run(
        ("uv", "build", "--wheel", "--out-dir", str(output)),
        cwd=REPO,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(output.glob("omnigent-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_flow_distribution_metadata_inventory_and_reproducible_wheel(tmp_path: Path) -> None:
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    guide = (REPO / "docs" / "flow" / "PACKAGING.md").read_text(encoding="utf-8")
    inventory = json.loads(
        (REPO / "docs" / "flow" / "dependency-inventory.json").read_text(encoding="utf-8")
    )

    assert 'flow-mcp = "omnigent.flow.mcp_server:main"' in pyproject
    assert 'license = "Apache-2.0"' in pyproject
    assert 'license-files = ["LICENSE", "NOTICE"]' in pyproject
    assert inventory["lockfile"] == "uv.lock"
    assert {item["name"] for item in inventory["flowRuntime"]} == {
        "dapr-ext-workflow",
        "jsonschema",
        "mcp",
        "pydantic",
    }
    for command in ("install", "upgrade", "uninstall", "flow-mcp", "sha256"):
        assert command in guide.lower()

    first = _build(tmp_path / "first")
    second = _build(tmp_path / "second")
    assert _digest(first) == _digest(second)
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
        contents = b"".join(archive.read(name) for name in names)
    assert any(name.endswith("entry_points.txt") for name in names)
    assert b"flow-mcp" in contents
    assert b"BEGIN PRIVATE KEY" not in contents
    assert b"sk-live-" not in contents


async def test_clean_wheel_install_launches_four_tool_mcp_server(
    tmp_path: Path, flow_discovery_env: dict[str, str]
) -> None:
    wheel = _build(tmp_path / "dist")
    target = tmp_path / "installed"
    subprocess.run(
        (
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(target),
            "--no-deps",
            str(wheel),
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "omnigent.flow.mcp_server"],
        cwd=tmp_path,
        env={**os.environ, **flow_discovery_env, "PYTHONPATH": str(target)},
    )

    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()

    assert [tool.name for tool in tools.tools] == [
        "propose_dag",
        "run_workflow",
        "get_workflow_status",
        "list_workflows",
    ]
