import json
from pathlib import Path

REPO = Path(__file__).parents[2]
HARNESS_DIR = REPO / "docs" / "flow" / "harnesses"
EXPECTED_TOOLS = [
    "propose_dag",
    "run_workflow",
    "get_workflow_status",
    "list_workflows",
]
EXPECTED_HARNESSES = {
    "copilot",
    "antigravity",
    "codex",
    "cursor",
    "opencode",
}


def test_five_harness_rollup_is_complete_and_traceable() -> None:
    index = json.loads((HARNESS_DIR / "index.json").read_text(encoding="utf-8"))
    guide = (HARNESS_DIR / "README.md").read_text(encoding="utf-8")

    assert index["schemaVersion"] == "1.0"
    assert index["transport"] == "stdio"
    assert index["command"] == "flow-mcp"
    assert index["tools"] == EXPECTED_TOOLS
    assert index["commonEnvironment"] == {"FLOW_LOG_LEVEL": "INFO"}
    assert set(index["harnesses"]) == EXPECTED_HARNESSES

    for name, item in index["harnesses"].items():
        assert item["status"] == "verified", name
        config_path = REPO / item["config"]
        evidence_path = REPO / item["evidence"]
        guide_path = REPO / item["guide"]
        assert config_path.is_file(), name
        assert evidence_path.is_file(), name
        assert guide_path.is_file(), name
        config_text = config_path.read_text(encoding="utf-8")
        for marker in ("BEGIN PRIVATE KEY", "sk-live-", "approvalToken"):
            assert marker not in config_text, name
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        assert evidence["tools"] == EXPECTED_TOOLS
        assert evidence["verifiedOn"]
        sources = evidence.get("officialSources", [evidence.get("officialSource")])
        assert all(source and source.startswith("https://") for source in sources)
        for path in (item["config"], item["evidence"], item["guide"]):
            assert Path(path).name in guide

    assert "Common contract" in guide
    assert "Harness-specific differences" in guide
    assert "No credential values" in guide
