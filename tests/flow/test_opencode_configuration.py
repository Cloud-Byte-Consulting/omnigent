import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.flow.native_conformance import SOURCE_DATE_EPOCH

REPO = Path(__file__).parents[2]
HARNESS_DIR = REPO / "docs" / "flow" / "harnesses"
EXPECTED_TOOLS = [
    "propose_dag",
    "run_workflow",
    "get_workflow_status",
    "list_workflows",
]
EXPECTED_SCENARIOS = {
    "tool-discovery",
    "three-node-approved-run",
    "invalid-graphs",
    "status-and-list",
    "bounded-expansion",
    "interruption-recovery",
    "provider-substitution",
}
EXPECTED_REDACTION = {
    "rawEventsPersisted": False,
    "secretsPersisted": False,
    "safeEvidenceRequired": True,
}
EXPECTED_ASSERTIONS = {
    "previewWithoutDispatch": "passed",
    "staleApprovalWithoutDispatch": "passed",
    "idempotentReplay": "passed",
    "capEnforcement": "passed",
    "dependencyOrder": "passed",
    "normalizedProviderOutput": "passed",
    "exactlyOnceRecovery": "passed",
}
EXPECTED_CANONICAL_RESULT = {
    "state": "succeeded",
    "nodes": ["A", "B", "C"],
    "dependencyOrder": "A and B succeeded before C dispatch",
    "fanInOutput": {"values": ["A", "B"]},
    "firstStartReused": False,
    "replayReused": True,
}
EXPECTED_SAFETY_RESULT = {
    "invalidGraphsDispatched": False,
    "staleApprovalDispatched": False,
    "boundedExpansionAcceptedNodes": 2,
    "boundedExpansionRound": 2,
    "boundedExpansionUsedTokens": 2,
    "deniedCap": "maxNodes",
    "normalizedProviderOutput": {"value": "same"},
    "recoveryEffectCounts": {"A": 1, "B": 1, "C": 1},
}
EXPECTED_BLOCKED_FAILURE = {
    "stage": "three-node-approved-run preview",
    "isolatedState": "fresh dynamically selected empty non-default Redis database",
    "expected": "one typed confirm:false result with approval_required and no durable dispatch",
    "observed": (
        "the expected result was followed by an additional native run_workflow call "
        "and an unexpected durable dispatch"
    ),
    "rawEventsPersisted": False,
    "durableRunIdPersisted": False,
}


def _assert_sha256(value: object) -> None:
    assert isinstance(value, str)
    assert len(value) == 64
    assert all(character in "0123456789abcdef" for character in value)


def _assert_opencode_conformance_evidence(evidence: dict[str, Any]) -> None:
    assert evidence["schemaVersion"] == "1.0"
    assert evidence["issue"] == "CLO-185"
    assert evidence["harness"] == "OpenCode"
    assert evidence["harnessVersion"] == "1.2.10"
    assert evidence["verifiedOn"]
    assert evidence["result"] in {"blocked", "passed"}
    assert evidence["flowVersion"]
    assert len(evidence["flowCommit"]) == 40
    assert all(character in "0123456789abcdef" for character in evidence["flowCommit"])
    assert evidence["fixtureRevision"] == "flow-conformance-1.0.0"
    assert evidence["transport"] == "stdio"
    assert evidence["tools"] == EXPECTED_TOOLS
    assert set(evidence["scenarios"]) == EXPECTED_SCENARIOS
    assert evidence["harnessBinary"]["version"] == "1.2.10"
    assert evidence["harnessBinary"]["path"] == "$HOME/.opencode/bin/opencode"
    _assert_sha256(evidence["harnessBinary"]["sha256"])
    assert evidence["model"]["id"] == "opencode/deepseek-v4-flash-free"
    assert evidence["model"]["catalogCommand"] == ("opencode models opencode | LC_ALL=C sort")
    _assert_sha256(evidence["model"]["catalogSha256"])
    assert evidence["probe"] == {
        "publicModelInvocation": "passed",
        "toolDiscovery": "passed",
        "scope": (
            "isolated single-tool readiness probes only; no workflow conformance "
            "result is inferred"
        ),
    }
    assert evidence["redaction"] == EXPECTED_REDACTION
    assert evidence["reproduce"] == (
        "FLOW_OPENCODE_E2E=1 uv run pytest -q -vv -s tests/flow/test_opencode_conformance_e2e.py"
    )

    if evidence["result"] == "blocked":
        assert evidence["artifact"] is None
        assert evidence["runIds"] is None
        assert evidence["assertions"] is None
        assert evidence["canonicalResult"] is None
        assert evidence["safetyResult"] is None
        assert evidence["blocker"]
        assert evidence["nativeFailure"] == EXPECTED_BLOCKED_FAILURE
        assert "preview-without-dispatch failed" in evidence["blocker"].lower()
        assert evidence["scenarios"]["tool-discovery"]["status"] == "passed"
        for scenario_name in EXPECTED_SCENARIOS - {"tool-discovery"}:
            assert evidence["scenarios"][scenario_name]["status"] == "blocked"
        return

    artifact = evidence["artifact"]
    assert artifact["sourceDateEpoch"] == SOURCE_DATE_EPOCH
    _assert_sha256(artifact["sha256"])
    assert artifact["firstBuildSha256"] == artifact["sha256"]
    assert artifact["secondBuildSha256"] == artifact["sha256"]
    assert all(item["status"] == "passed" for item in evidence["scenarios"].values())
    assert evidence["blocker"] is None
    assert evidence["nativeFailure"] is None
    assert evidence["assertions"] == EXPECTED_ASSERTIONS
    assert evidence["canonicalResult"] == EXPECTED_CANONICAL_RESULT
    assert evidence["safetyResult"] == EXPECTED_SAFETY_RESULT

    run_ids = evidence["runIds"]
    assert set(run_ids) == {
        "canonical",
        "boundedExpansion",
        "boundedExpansionDenied",
        "interruptionRecovery",
        "providerSubstitution",
    }
    flattened_run_ids = [
        run_ids["canonical"],
        run_ids["boundedExpansion"],
        run_ids["boundedExpansionDenied"],
        run_ids["interruptionRecovery"],
        *run_ids["providerSubstitution"],
    ]
    assert all(isinstance(run_id, str) and run_id for run_id in flattened_run_ids)
    assert len(flattened_run_ids) == len(set(flattened_run_ids)) == 6


def _passing_opencode_evidence() -> dict[str, Any]:
    evidence = json.loads(
        (HARNESS_DIR / "opencode-conformance-evidence.json").read_text(encoding="utf-8")
    )
    passed = deepcopy(evidence)
    passed.update(
        {
            "result": "passed",
            "artifact": {
                "sourceDateEpoch": SOURCE_DATE_EPOCH,
                "sha256": "a" * 64,
                "firstBuildSha256": "a" * 64,
                "secondBuildSha256": "a" * 64,
            },
            "assertions": deepcopy(EXPECTED_ASSERTIONS),
            "canonicalResult": deepcopy(EXPECTED_CANONICAL_RESULT),
            "safetyResult": deepcopy(EXPECTED_SAFETY_RESULT),
            "nativeFailure": None,
            "blocker": None,
            "runIds": {
                "canonical": "run-1",
                "boundedExpansion": "run-2",
                "boundedExpansionDenied": "run-3",
                "interruptionRecovery": "run-4",
                "providerSubstitution": ["run-5", "run-6"],
            },
        }
    )
    for scenario in passed["scenarios"].values():
        scenario["status"] = "passed"
    passed["scenarios"]["three-node-approved-run"].pop("evidence", None)
    return passed


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
    assert "empty disposable Redis container" in guide
    for marker in ("BEGIN PRIVATE KEY", "sk-live-", "approvalToken"):
        assert marker not in config_text


def test_opencode_native_conformance_evidence_is_honest_and_redacted() -> None:
    evidence_text = (HARNESS_DIR / "opencode-conformance-evidence.json").read_text(
        encoding="utf-8"
    )
    evidence = json.loads(evidence_text)

    _assert_opencode_conformance_evidence(evidence)
    for marker in (
        "approvalToken",
        "approval_token",
        "BEGIN PRIVATE KEY",
        "sk-live-",
        "temporary-local-opencode-e2e-key",
    ):
        assert marker not in evidence_text


def test_opencode_native_conformance_pass_schema_requires_immutable_proof() -> None:
    _assert_opencode_conformance_evidence(_passing_opencode_evidence())


def test_opencode_pass_evidence_rejects_a_forged_source_date_epoch() -> None:
    passed = _passing_opencode_evidence()
    passed["artifact"]["sourceDateEpoch"] = "garbage"

    with pytest.raises(AssertionError):
        _assert_opencode_conformance_evidence(passed)


def test_opencode_pass_evidence_rejects_unsafe_redaction() -> None:
    passed = _passing_opencode_evidence()
    passed["redaction"]["secretsPersisted"] = True

    with pytest.raises(AssertionError):
        _assert_opencode_conformance_evidence(passed)


def test_opencode_pass_evidence_rejects_missing_safety_assertion() -> None:
    passed = _passing_opencode_evidence()
    del passed["assertions"]["exactlyOnceRecovery"]

    with pytest.raises(AssertionError):
        _assert_opencode_conformance_evidence(passed)


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
