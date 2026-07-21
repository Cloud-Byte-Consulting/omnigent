import json
from pathlib import Path

REPO = Path(__file__).parents[2]
GUIDE = REPO / "docs" / "flow" / "dapr-inspection.md"
EVIDENCE = REPO / "docs" / "flow" / "dapr-inspection-evidence.json"


def test_supported_dapr_inspection_view_is_reproducible_and_gap_mapped() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    assert evidence["daprCliVersion"] == "1.18.0"
    assert evidence["daprRuntimeVersion"] == "1.18.1"
    assert evidence["verifiedOn"] == "2026-07-20"
    assert len(evidence["officialSources"]) >= 3
    assert evidence["inspectionView"] == "Dapr Workflow CLI"
    assert evidence["dashboardCommandAvailable"] is False
    assert evidence["canonicalFixtureTest"].endswith(
        "test_three_node_dag_recovers_mid_wave_without_duplicate_effects"
    )
    assert evidence["failedActivityFixtureTest"].endswith(
        "test_failed_activity_is_visible_in_safe_dapr_and_flow_views"
    )

    commands = (
        "python -m omnigent.flow.local_dapr start",
        "python -m omnigent.flow.local_dapr inspect-list",
        "python -m omnigent.flow.local_dapr inspect-history <run-id>",
        "python -m omnigent.flow.local_dapr status",
    )
    for command in commands:
        assert command in guide
    for field in (
        "run state",
        "activity history",
        "timing",
        "attempts",
        "failure",
        "component health",
        "approval",
        "caps",
        "usage",
        "expansions",
    ):
        assert field in guide.lower()
    assert "get_workflow_status" in guide
    assert "dapr dashboard" in guide
    assert "terminal 1" in guide.lower()
    assert "terminal 2" in guide.lower()
    assert "raw dapr json" in guide.lower()
