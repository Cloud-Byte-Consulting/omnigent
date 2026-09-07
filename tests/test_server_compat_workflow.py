"""Release-less forks have no compatibility baseline; real releases still run."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/server-compat.yml"


@pytest.mark.parametrize(
    ("tags", "expected", "git_exit"),
    [
        ("", "", 0),
        ("v0.4.0.dev0\nv0.3.0rc1\nv0.3.0a1\nnightly\n", "", 0),
        ("v0.4.0.dev0\nv0.3.0\nv0.2.0\n", "v0.3.0", 0),
        ("0.3.0\nv0.2.0\n", "0.3.0", 0),
        ("v0.3.0\n" + "v0.2.0\n" * 10000, "v0.3.0", 0),
        ("", "", 128),
    ],
    ids=["no-tags", "prereleases-only", "latest-final", "no-v-prefix", "many-tags", "git-error"],
)
def test_resolve_latest_stable_tag(
    tmp_path: Path, tags: str, expected: str, git_exit: int
) -> None:
    # The mock preserves git's version-sorted order, including a pipe-sized listing.
    tag_file = tmp_path / "tags"
    tag_file.write_text(tags)
    git = tmp_path / "git"
    git.write_text('#!/bin/sh\ncat "$TEST_TAGS"\nexit "$TEST_GIT_EXIT"\n')
    git.chmod(0o755)
    output = tmp_path / "output"
    workflow = yaml.safe_load(WORKFLOW.read_text())
    step = next(s for s in workflow["jobs"]["resolve-latest"]["steps"] if s.get("id") == "tag")
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "TEST_TAGS": str(tag_file),
            "TEST_GIT_EXIT": str(git_exit),
            "GITHUB_OUTPUT": str(output),
        },
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == git_exit, result.stderr
    if git_exit:
        assert not output.exists()
        return
    assert output.read_text() == f"latest_tag={expected}\n"
    if not expected:
        assert "no released version to compare against" in result.stdout


def test_compat_smoke_requires_a_released_baseline() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    smoke_jobs = {name: job for name, job in jobs.items() if name.startswith("compat-smoke-")}
    assert len(smoke_jobs) == 4
    for job in smoke_jobs.values():
        assert job["needs"] == "resolve-latest"
        assert job["if"] == "needs.resolve-latest.outputs.latest_tag != ''"
