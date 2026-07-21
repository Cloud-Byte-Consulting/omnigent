import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from omnigent.flow.local_dapr import APP_ID, CLI_VERSION, cli_version

pytestmark = pytest.mark.skipif(shutil.which("dapr") is None, reason="Dapr CLI is not installed")


def test_real_cli_docker_and_state_store_boundary() -> None:
    version = subprocess.run(
        ("dapr", "--version"),
        check=True,
        capture_output=True,
        text=True,
    )
    docker = subprocess.run(
        ("docker", "info", "--format", "{{json .ServerVersion}}"),
        check=True,
        capture_output=True,
        text=True,
    )
    component = yaml.safe_load(
        (
            Path(__file__).parents[2]
            / "deploy"
            / "flow"
            / "dapr"
            / "components"
            / "statestore.yaml"
        ).read_text()
    )

    assert cli_version(version.stdout) == CLI_VERSION
    assert json.loads(docker.stdout)
    assert component["spec"]["type"] == "state.redis"
    assert {item["name"]: item["value"] for item in component["spec"]["metadata"]}[
        "actorStateStore"
    ] == "true"
    assert component["scopes"] == [APP_ID]
