import json
import sys
from pathlib import Path
from tempfile import TemporaryFile
from typing import Any

from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from omnigent.flow.contracts import published_schema
from omnigent.flow.validation import validate_dag, validate_expansion

ROOT = Path(__file__).parent / "fixtures" / "conformance"
SERVER = Path(__file__).parent / "fixtures" / "mcp_contract_server.py"
HARNESSES = {
    "github-copilot": "CLO-181",
    "google-antigravity": "CLO-183",
    "openai-codex": "CLO-182",
    "cursor-agent": "CLO-184",
    "opencode": "CLO-185",
}


def load(name: str) -> dict[str, Any]:
    value = json.loads((ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def structured(result: Any) -> dict[str, Any]:
    value = result.structuredContent
    assert isinstance(value, dict)
    return value


def test_every_portable_fixture_matches_its_canonical_contract() -> None:
    workflow = load("workflow.json")
    scenarios = load("scenarios.json")

    Draft202012Validator(published_schema("dag-spec")).validate(workflow["dagSpec"])
    assert validate_dag(workflow["dagSpec"]).is_valid
    assert len(workflow["providerResponses"]) == 3

    for case in scenarios["invalidGraphs"]:
        result = validate_dag(case["dagSpec"])
        assert [error.code for error in result.errors] == case["expectedErrors"]

    expansion = scenarios["capAndExpansion"]
    base = validate_dag(expansion["baseDag"]).dag
    assert base is not None
    accepted = validate_expansion(
        base,
        expansion["accepted"],
        succeeded_node_ids={"A"},
        current_round=1,
    )
    denied = validate_expansion(
        base,
        expansion["denied"],
        succeeded_node_ids={"A"},
        current_round=1,
    )
    assert accepted.is_valid
    assert [error.code for error in denied.errors] == expansion["deniedErrors"]


def test_manifest_is_complete_language_neutral_and_shared_by_every_harness() -> None:
    manifest = load("manifest.json")
    required_fields = {
        "id",
        "gherkin",
        "command",
        "assertions",
        "evidence",
        "requiredTestLevel",
        "fixtures",
        "nonHarnessSpecific",
    }

    revision = "flow-conformance-1.0.0"
    assert manifest["fixtureRevision"] == revision
    assert manifest["harnessIssues"] == HARNESSES
    assert manifest["scenarios"]
    scenario_ids = [scenario["id"] for scenario in manifest["scenarios"]]
    assert len(scenario_ids) == len(set(scenario_ids))
    for scenario in manifest["scenarios"]:
        assert set(scenario) == required_fields
        assert scenario["requiredTestLevel"] in {"unit", "integration", "end-to-end"}
        assert scenario["assertions"]
        for fixture in scenario["fixtures"]:
            assert (ROOT / fixture).is_file()

    for path in ROOT.iterdir():
        assert path.suffix == ".json"
        text = path.read_text(encoding="utf-8")
        assert json.loads(text)["fixtureRevision"] == revision
        assert "omnigent." not in text
        assert ".py" not in text


async def test_non_harness_scenarios_dry_run_through_real_mcp_server() -> None:
    workflow = load("workflow.json")
    expected = load("expected.json")
    parameters = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    with TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        async with stdio_client(parameters, errlog=stderr) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                preview = await session.call_tool(
                    "run_workflow",
                    {
                        "dag_spec": workflow["dagSpec"],
                        "confirm": False,
                        "idempotency_key": workflow["approval"]["idempotencyKey"],
                    },
                )
                preview_value = structured(preview)
                assert preview_value["status"] == expected["preview"]["status"]
                arguments = {
                    "dag_spec": workflow["dagSpec"],
                    "approval_token": preview_value["approvalToken"],
                    "confirm": True,
                    "idempotency_key": workflow["approval"]["idempotencyKey"],
                }
                started = structured(await session.call_tool("run_workflow", arguments))
                replayed = structured(await session.call_tool("run_workflow", arguments))
                assert started["runId"] == replayed["runId"] == expected["start"]["runId"]
                assert replayed["reused"] is True

                listed = structured(
                    await session.call_tool("list_workflows", {"status": "queued"})
                )
                assert listed["visibleCount"] == expected["list"]["visibleCount"]
