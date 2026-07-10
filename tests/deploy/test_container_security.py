"""Static security contracts for published container images."""

from pathlib import Path

_DOCKERFILE = Path(__file__).resolve().parents[2] / "deploy/docker/Dockerfile"


def test_server_runtime_declares_nonroot_user_with_owned_artifact_dir() -> None:
    text = _DOCKERFILE.read_text()
    runtime = text.split("FROM python:${PYTHON_VERSION}-slim AS runtime", maxsplit=1)[1]

    assert "USER 10001:10001" in runtime
    assert "chown -R 10001:10001 /data" in runtime
