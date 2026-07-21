"""Local Dapr operations for the Flow development runtime."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

CLI_VERSION = "1.18.0"
RUNTIME_VERSION = "1.18.1"
APP_ID = "omnigent-flow"
HTTP_PORT = 3510
GRPC_PORT = 50101
SCHEDULER_VOLUME = "dapr_scheduler"


def init_command() -> tuple[str, ...]:
    return (
        "dapr",
        "init",
        "--runtime-version",
        RUNTIME_VERSION,
        "--scheduler-volume",
        SCHEDULER_VOLUME,
    )


def start_command(repo: Path, *, python: str = sys.executable) -> tuple[str, ...]:
    return (
        "dapr",
        "run",
        "--app-id",
        APP_ID,
        "--dapr-http-port",
        str(HTTP_PORT),
        "--dapr-grpc-port",
        str(GRPC_PORT),
        "--resources-path",
        str(repo / "deploy" / "flow" / "dapr" / "components"),
        "--",
        python,
        "-m",
        "omnigent.flow.smoke_worker",
    )


def clean_reset_commands(*, confirmed: bool) -> tuple[tuple[str, ...], ...]:
    if not confirmed:
        raise ValueError("clean-reset deletes local Dapr state; pass --yes")
    return (
        ("dapr", "stop", "--app-id", APP_ID),
        ("dapr", "uninstall", "--all"),
        init_command(),
    )


def cli_version(output: str) -> str:
    match = re.search(r"CLI version:\s*(?:version:)?\s*([0-9]+(?:\.[0-9]+){2})", output)
    if not match:
        raise ValueError("could not read Dapr CLI version")
    return match.group(1)


def check_prerequisites() -> None:
    version = subprocess.run(
        ("dapr", "--version"),
        check=True,
        capture_output=True,
        text=True,
    )
    found = cli_version(version.stdout)
    if found != CLI_VERSION:
        raise RuntimeError(f"Dapr CLI {CLI_VERSION} required; found {found}")
    subprocess.run(("docker", "info"), check=True, capture_output=True)


def readiness() -> dict[str, bool]:
    status = {
        "runtimeVersion": False,
        "sidecar": False,
        "stateStore": False,
        "workflowService": False,
    }
    try:
        listed = subprocess.run(
            ("dapr", "list", "--output", "json"),
            check=True,
            capture_output=True,
            text=True,
        )
        apps = json.loads(listed.stdout)
        status["sidecar"] = any(app.get("appId") == APP_ID for app in apps)
        with urlopen(f"http://127.0.0.1:{HTTP_PORT}/v1.0/metadata", timeout=2) as response:
            metadata = json.load(response)
        status["runtimeVersion"] = metadata.get("runtimeVersion") == RUNTIME_VERSION
        components = metadata.get("components", [])
        status["stateStore"] = any(
            component.get("name") == "flowstatestore" and component.get("type") == "state.redis"
            for component in components
        )
        status["workflowService"] = metadata.get("workflows", {}).get("connectedWorkers", 0) > 0
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        pass
    return status


def _run_all(commands: tuple[tuple[str, ...], ...]) -> None:
    for command in commands:
        subprocess.run(command, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("init")
    subparsers.add_parser("start")
    subparsers.add_parser("stop")
    reset = subparsers.add_parser("clean-reset")
    reset.add_argument("--yes", action="store_true")
    subparsers.add_parser("status")
    history = subparsers.add_parser("history")
    history.add_argument("instance_id")
    args = parser.parse_args(argv)

    repo = Path(__file__).parents[2]
    if args.action in {"init", "start", "clean-reset"}:
        check_prerequisites()
    if args.action == "init":
        _run_all((init_command(),))
    elif args.action == "start":
        _run_all((start_command(repo),))
    elif args.action == "stop":
        _run_all((("dapr", "stop", "--app-id", APP_ID),))
    elif args.action == "clean-reset":
        _run_all(clean_reset_commands(confirmed=args.yes))
    elif args.action == "status":
        status = readiness()
        print(json.dumps(status, sort_keys=True))
        return 0 if all(status.values()) else 1
    else:
        _run_all((("dapr", "workflow", "history", args.instance_id, "--app-id", APP_ID),))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
