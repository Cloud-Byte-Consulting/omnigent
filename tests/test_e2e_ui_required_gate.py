"""The UI coverage gate only needs judge configuration for UI changes."""

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("path", ["docs/desktop-linux-plan.md", "web/src/App.tsx"])
def test_judge_configuration_is_only_required_for_ui_changes(tmp_path: Path, path: str) -> None:
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\nprintf "modified\\t%s\\n" "$TEST_CHANGED_PATH"\n')
    gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "REPO": "example/repository",
        "PR": "1",
        "TEST_CHANGED_PATH": path,
        "E2E_UI_JUDGE_MODEL": "",
        "OPENAI_BASE_URL": "",
        "OPENAI_API_KEY": "",
    }
    result = subprocess.run(
        ["bash", str(REPO / ".github/scripts/e2e-ui-required/check.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if path.startswith("web/"):
        assert result.returncode != 0
        assert "Set OMNIGENT_CI_E2E_JUDGE_MODEL" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert "e2e_ui coverage not required" in result.stdout


def test_workflow_does_not_require_judge_before_running_gate() -> None:
    workflow = (REPO / ".github/workflows/e2e-ui-required.yml").read_text()
    assert "${E2E_UI_JUDGE_MODEL:?" not in workflow
