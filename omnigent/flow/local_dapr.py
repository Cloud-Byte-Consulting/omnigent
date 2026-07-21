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
_LIST_FIELDS = (
    "namespace",
    "appID",
    "name",
    "instanceID",
    "created",
    "lastUpdate",
    "runtimeStatus",
)
_HISTORY_FIELDS = (
    "namespace",
    "appID",
    "play",
    "type",
    "name",
    "eventId",
    "timestamp",
    "elapsed",
    "status",
    "router",
    "executionId",
)


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


def safe_workflow_list(value: object) -> list[dict[str, object]]:
    """Project Dapr workflow rows without inputs, outputs, or failure messages."""
    rows = value if isinstance(value, list) else []
    result: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        projected = {key: row[key] for key in _LIST_FIELDS if _safe_scalar(row.get(key))}
        flow_status = _safe_flow_status(row.get("customStatus"))
        if flow_status:
            projected["flowStatus"] = flow_status
        result.append(projected)
    return result


def safe_workflow_history(value: object) -> list[dict[str, object]]:
    """Project Dapr history rows without event details, attributes, or payloads."""
    rows = value if isinstance(value, list) else []
    return [
        {key: row[key] for key in _HISTORY_FIELDS if _safe_scalar(row.get(key))}
        for row in rows
        if isinstance(row, dict)
    ]


def _safe_flow_status(value: object) -> dict[str, object]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    result: dict[str, object] = {}
    status = decoded.get("status")
    if isinstance(status, str):
        result["status"] = status
    nodes = decoded.get("nodes")
    if not isinstance(nodes, dict):
        return result
    safe_nodes: dict[str, object] = {}
    for node_id, state in nodes.items():
        if not isinstance(node_id, str) or not isinstance(state, dict):
            continue
        node: dict[str, object] = {}
        node_status = state.get("status")
        if isinstance(node_status, str):
            node["status"] = node_status
        attempt = state.get("attempt")
        if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0:
            node["attempt"] = attempt
        failure = state.get("failure")
        if isinstance(failure, dict):
            safe_failure: dict[str, object] = {}
            category = failure.get("category")
            if isinstance(category, str):
                safe_failure["category"] = category
            retryable = failure.get("retryable")
            if isinstance(retryable, bool):
                safe_failure["retryable"] = retryable
            if safe_failure:
                node["failure"] = safe_failure
        safe_nodes[node_id] = node
    result["nodes"] = safe_nodes
    return result


def _safe_scalar(value: object) -> bool:
    return isinstance(value, (str, int, float, bool)) and not isinstance(value, bytes)


def _inspect(command: tuple[str, ...], projector: object) -> None:
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    decoded = json.loads(completed.stdout)
    assert callable(projector)
    print(json.dumps(projector(decoded), sort_keys=True))


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
    subparsers.add_parser("inspect-list")
    inspect_history = subparsers.add_parser("inspect-history")
    inspect_history.add_argument("instance_id")
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
    elif args.action == "inspect-list":
        _inspect(
            ("dapr", "workflow", "list", "--app-id", APP_ID, "--output", "json"),
            safe_workflow_list,
        )
    elif args.action == "inspect-history":
        _inspect(
            (
                "dapr",
                "workflow",
                "history",
                args.instance_id,
                "--app-id",
                APP_ID,
                "--output",
                "json",
            ),
            safe_workflow_history,
        )
    else:
        _run_all((("dapr", "workflow", "history", args.instance_id, "--app-id", APP_ID),))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
