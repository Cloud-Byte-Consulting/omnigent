"""Security results remain visible after other CI jobs fill the first page."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_security_gate_reads_scan_from_later_page(tmp_path: Path, conclusion: str) -> None:
    page = {"check_runs": [{"name": "Other CI check"}] * 100}
    scan_page = {
        "check_runs": [
            {
                "name": "Security Scan",
                "started_at": "2026-01-01T00:00:00Z",
                "status": "completed",
                "conclusion": conclusion,
                "html_url": "https://example.com/security-scan",
            }
        ]
    }
    (tmp_path / "first.json").write_text(json.dumps(page))
    (tmp_path / "pages.json").write_text(json.dumps([page, scan_page]))
    gh = tmp_path / "gh"
    gh.write_text(
        '#!/bin/sh\ncase "$*" in\n'
        "  *actions/runs*) exit 0 ;;\n"
        '  *--paginate*--slurp*) cat "$TEST_DATA/pages.json" ;;\n'
        '  *) cat "$TEST_DATA/first.json" ;;\nesac\n'
    )
    gh.chmod(0o755)
    # A visible completed scan must conclude immediately, not exhaust the poll budget.
    sleep = tmp_path / "sleep"
    sleep.write_text("#!/bin/sh\nexit 97\n")
    sleep.chmod(0o755)
    workflow_path = Path(__file__).resolve().parents[1] / ".github/workflows/security-gate.yml"
    workflow = yaml.safe_load(workflow_path.read_text())
    step = next(
        s
        for s in workflow["jobs"]["gate"]["steps"]
        if s["name"] == "Wait for Security Scan result"
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "TEST_DATA": str(tmp_path),
            "REPO": "example/repository",
            "HEAD_SHA": "test-head",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == (0 if conclusion == "success" else 1), result.stderr
    assert f"Security Scan concluded: {conclusion}" in result.stdout
