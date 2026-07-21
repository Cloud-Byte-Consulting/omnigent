from pathlib import Path

import pytest

from omnigent.flow.local_dapr import (
    APP_ID,
    CLI_VERSION,
    GRPC_PORT,
    HTTP_PORT,
    RUNTIME_VERSION,
    clean_reset_commands,
    init_command,
    start_command,
)


def test_initialize_command_pins_runtime_and_persistent_scheduler() -> None:
    assert init_command() == (
        "dapr",
        "init",
        "--runtime-version",
        RUNTIME_VERSION,
        "--scheduler-volume",
        "dapr_scheduler",
    )
    assert CLI_VERSION == "1.18.0"


def test_start_command_has_stable_identity_ports_and_resources() -> None:
    command = start_command(Path("/repo"), python="python3")

    assert command == (
        "dapr",
        "run",
        "--app-id",
        APP_ID,
        "--dapr-http-port",
        str(HTTP_PORT),
        "--dapr-grpc-port",
        str(GRPC_PORT),
        "--resources-path",
        "/repo/deploy/flow/dapr/components",
        "--",
        "python3",
        "-m",
        "omnigent.flow.smoke_worker",
    )


def test_clean_reset_requires_explicit_confirmation() -> None:
    with pytest.raises(ValueError, match="--yes"):
        clean_reset_commands(confirmed=False)

    assert clean_reset_commands(confirmed=True) == (
        ("dapr", "stop", "--app-id", APP_ID),
        ("dapr", "uninstall", "--all"),
        init_command(),
    )
