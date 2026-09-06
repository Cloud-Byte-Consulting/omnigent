"""Shared native coding-harness conformance scenarios and runtime helpers."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote
from urllib.request import urlopen

import pytest
from dapr.ext.workflow import DaprWorkflowClient

from omnigent.flow.local_dapr import APP_ID, GRPC_PORT, HTTP_PORT, readiness, start_command
from omnigent.flow.orchestration import derive_node_execution_id

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "conformance"
SOURCE_DATE_EPOCH = "1767225600"


class NativeToolExecution(Protocol):
    """Structural result shared by native harness adapters."""

    @property
    def tool_name(self) -> str: ...

    @property
    def arguments(self) -> dict[str, Any]: ...

    @property
    def structured_result(self) -> dict[str, Any]: ...

    def safe_evidence(self) -> dict[str, Any]: ...


class NativeToolCaller(Protocol):
    """Call one canonical Flow MCP tool through a native harness."""

    def __call__(
        self,
        executable: str,
        config: Path,
        tool: str,
        arguments: dict[str, Any],
        *,
        cwd: Path,
        env: dict[str, str],
    ) -> NativeToolExecution: ...


def exercise_safety_and_provider_scenarios(
    call: NativeToolCaller,
    executable: str,
    config: Path,
    *,
    cwd: Path,
    env: dict[str, str],
) -> tuple[list[NativeToolExecution], list[str]]:
    scenarios = load_fixture("scenarios.json")
    executions: list[NativeToolExecution] = []
    before = catalog_run_ids()
    for case in scenarios["invalidGraphs"]:
        rejected = call(
            executable,
            config,
            "run_workflow",
            {"dag_spec": case["dagSpec"], "confirm": False},
            cwd=cwd,
            env=env,
        )
        assert rejected.structured_result["error"]["code"] == "invalid_input"
        assert case["expectedErrors"][0] in rejected.structured_result["error"]["message"]
        executions.append(rejected)

    canonical = load_fixture("workflow.json")["dagSpec"]
    stale_preview = call(
        executable,
        config,
        "run_workflow",
        {"dag_spec": canonical, "confirm": False},
        cwd=cwd,
        env=env,
    )
    stale_dag = json.loads(json.dumps(canonical))
    stale_dag["nodes"][0]["instructions"] = "Changed after approval"
    stale = call(
        executable,
        config,
        "run_workflow",
        {
            "dag_spec": stale_dag,
            "approval_token": stale_preview.structured_result["approvalToken"],
            "confirm": True,
        },
        cwd=cwd,
        env=env,
    )
    assert stale.structured_result["error"]["code"] == "approval_invalid"
    assert catalog_run_ids() == before
    executions.extend((stale_preview, stale))

    substitution = scenarios["providerSubstitution"]
    normalized: list[dict[str, Any]] = []
    provider_run_ids: list[str] = []
    for model in substitution["adapters"]:
        provider_dag = _provider_substitution_dag(model, substitution["expectedNormalizedOutput"])
        provider_preview, provider_started = _preview_and_start(
            call,
            executable,
            config,
            provider_dag,
            cwd=cwd,
            env=env,
            idempotency_key=f"native-provider-{model}-{secrets.token_hex(12)}",
        )
        provider_run_id = cast(str, provider_started.structured_result["runId"])
        provider_run_ids.append(provider_run_id)
        wait_for_completion(provider_run_id)
        provider_status = call(
            executable,
            config,
            "get_workflow_status",
            {"run_id": provider_run_id},
            cwd=cwd,
            env=env,
        )
        node = provider_status.structured_result["nodes"]["same"]
        output = workflow_output(provider_run_id)["nodes"]["same"]["output"]
        assert provider_status.structured_result["state"] == "succeeded"
        assert f"{node['provider']}:{node['model']}" == model
        assert output == substitution["expectedNormalizedOutput"]
        normalized.append(
            {
                "state": node["state"],
                "usage": node["usage"],
                "output": output,
            }
        )
        executions.extend((provider_preview, provider_started, provider_status))
    assert normalized[0] == normalized[1]
    return executions, provider_run_ids


def _provider_substitution_dag(
    model: str,
    expected_output: dict[str, Any],
) -> dict[str, Any]:
    return {
        "version": "1.0",
        "nodes": [
            {
                "id": "same",
                "instructions": "Produce the shared provider-neutral deterministic value",
                "model": model,
                "outputSchema": {
                    "type": "object",
                    "properties": {"value": {"const": expected_output["value"]}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            }
        ],
        "caps": {
            "maxNodes": 1,
            "maxRounds": 1,
            "maxConcurrent": 1,
            "tokenBudget": 1,
        },
    }


def exercise_expansion_scenario(
    call: NativeToolCaller,
    executable: str,
    config: Path,
    *,
    cwd: Path,
    env: dict[str, str],
) -> tuple[list[NativeToolExecution], str, str]:
    dag = load_fixture("scenarios.json")["capAndExpansion"]["baseDag"]
    preview, started = _preview_and_start(
        call,
        executable,
        config,
        dag,
        cwd=cwd,
        env=env,
        idempotency_key=f"native-expansion-{secrets.token_hex(12)}",
    )
    run_id = cast(str, started.structured_result["runId"])
    wait_for_completion(run_id)
    status = call(
        executable,
        config,
        "get_workflow_status",
        {"run_id": run_id},
        cwd=cwd,
        env=env,
    )
    utilization = status.structured_result["caps"]["utilization"]
    assert status.structured_result["state"] == "succeeded"
    assert utilization["acceptedNodes"] == 2
    assert utilization["currentRound"] == 2
    assert utilization["usedTokens"] == 2
    assert status.structured_result["expansionHistory"][0]["type"] == "expansion"

    denied_dag = json.loads(json.dumps(dag))
    denied_dag["caps"]["maxNodes"] = 1
    denied_preview, denied_started = _preview_and_start(
        call,
        executable,
        config,
        denied_dag,
        cwd=cwd,
        env=env,
        idempotency_key=f"native-expansion-denied-{secrets.token_hex(12)}",
    )
    denied_run_id = cast(str, denied_started.structured_result["runId"])
    wait_for_completion(denied_run_id)
    denied_status = call(
        executable,
        config,
        "get_workflow_status",
        {"run_id": denied_run_id},
        cwd=cwd,
        env=env,
    )
    denied_utilization = denied_status.structured_result["caps"]["utilization"]
    assert denied_utilization["acceptedNodes"] == 1
    assert denied_utilization["currentRound"] == 1
    assert any(
        event["type"] in {"expansion_rejected", "cap_denial"}
        and event["metadata"].get("cap") == "maxNodes"
        for event in denied_status.structured_result["expansionHistory"]
    )
    return (
        [
            preview,
            started,
            status,
            denied_preview,
            denied_started,
            denied_status,
        ],
        run_id,
        denied_run_id,
    )


def exercise_recovery_scenario(
    call: NativeToolCaller,
    executable: str,
    config: Path,
    *,
    repo: Path,
    cwd: Path,
    installed: Path,
    env: dict[str, str],
    worker_env: dict[str, str],
    worker: subprocess.Popen[bytes],
    resources_path: Path | None = None,
) -> tuple[list[NativeToolExecution], str, subprocess.Popen[bytes]]:
    dag = load_fixture("workflow.json")["dagSpec"]
    preview, started = _preview_and_start(
        call,
        executable,
        config,
        dag,
        cwd=cwd,
        env=env,
        idempotency_key=f"native-recovery-{secrets.token_hex(12)}",
    )
    run_id = cast(str, started.structured_result["runId"])
    wait_until(
        lambda: (
            (effect(run_id, "A") or {}).get("completed") is True
            and (effect(run_id, "B") or {}).get("completed") is False
        ),
        "recovery checkpoint",
    )
    crash_worker(worker)
    restarted = start_installed_worker(
        repo,
        cwd,
        installed,
        worker_env,
        resources_path=resources_path,
    )
    try:
        wait_until(lambda: all(readiness().values()), "restarted Dapr worker")
        wait_for_completion(run_id)
        status = call(
            executable,
            config,
            "get_workflow_status",
            {"run_id": run_id},
            cwd=cwd,
            env=env,
        )
        assert_succeeded_fan_in(status.structured_result, run_id)
        assert all((effect(run_id, node) or {})["effectCount"] == 1 for node in ("A", "B", "C"))
    except BaseException:
        stop_worker(restarted)
        raise
    return [preview, started, status], run_id, restarted


def _preview_and_start(
    call: NativeToolCaller,
    executable: str,
    config: Path,
    dag: dict[str, Any],
    *,
    cwd: Path,
    env: dict[str, str],
    idempotency_key: str,
) -> tuple[NativeToolExecution, NativeToolExecution]:
    preview = call(
        executable,
        config,
        "run_workflow",
        {"dag_spec": dag, "confirm": False, "idempotency_key": idempotency_key},
        cwd=cwd,
        env=env,
    )
    assert preview.structured_result["status"] == "approval_required"
    started = call(
        executable,
        config,
        "run_workflow",
        {
            "dag_spec": dag,
            "approval_token": preview.structured_result["approvalToken"],
            "confirm": True,
            "idempotency_key": idempotency_key,
        },
        cwd=cwd,
        env=env,
    )
    assert started.structured_result["reused"] is False
    return preview, started


def run_process_group(
    command: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def build_wheel(repo: Path, output: Path) -> Path:
    output.mkdir()
    environment = {**os.environ, "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH}
    completed = subprocess.run(
        ("uv", "build", "--wheel", "--out-dir", str(output)),
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError("immutable Flow wheel build failed")
    wheels = list(output.glob("omnigent-*.whl"))
    if len(wheels) != 1:
        raise AssertionError("wheel build did not produce exactly one Flow artifact")
    return wheels[0]


def install_wheel(wheel: Path, target: Path) -> None:
    subprocess.run(
        ("uv", "venv", str(target)),
        capture_output=True,
        text=True,
        check=True,
    )
    site_packages = (
        target
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    dependency_site = Path(pytest.__file__).parents[1]
    (site_packages / "flow-e2e-dependencies.pth").write_text(
        f"{dependency_site}\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        (
            "uv",
            "pip",
            "install",
            "--python",
            str(target / "bin" / "python"),
            "--no-deps",
            str(wheel),
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError("immutable Flow wheel install failed")
    if not (target / "bin" / "flow-mcp").is_file():
        raise AssertionError("installed wheel is missing the flow-mcp entrypoint")


def start_installed_worker(
    repo: Path,
    cwd: Path,
    installed: Path,
    flow_environment: dict[str, str],
    *,
    resources_path: Path | None = None,
) -> subprocess.Popen[bytes]:
    environment = worker_environment(flow_environment)
    command = list(start_command(repo, python=str(installed / "bin" / "python")))
    if resources_path is not None:
        command[command.index("--resources-path") + 1] = str(resources_path)
    return subprocess.Popen(
        tuple(command),
        cwd=cwd,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def restart_worker(
    process: subprocess.Popen[bytes],
    repo: Path,
    cwd: Path,
    installed: Path,
    environment: dict[str, str],
    *,
    resources_path: Path | None = None,
) -> subprocess.Popen[bytes]:
    stop_worker(process)
    restarted = start_installed_worker(
        repo,
        cwd,
        installed,
        environment,
        resources_path=resources_path,
    )
    try:
        wait_until(lambda: all(readiness().values()), "restarted Dapr worker")
    except BaseException:
        stop_worker(restarted)
        raise
    return restarted


def worker_environment(flow_environment: dict[str, str]) -> dict[str, str]:
    allowlist = (
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
    )
    environment = {name: os.environ[name] for name in allowlist if name in os.environ}
    environment.update(flow_environment)
    environment.setdefault("FLOW_FAKE_DELAY_SECONDS", "0")
    return environment


def wait_for_completion(run_id: str) -> None:
    client = DaprWorkflowClient(host="127.0.0.1", port=str(GRPC_PORT))
    try:
        completed = client.wait_for_workflow_completion(run_id, timeout_in_seconds=45)
    finally:
        cast(Any, client).close()
    assert completed is not None
    assert completed.runtime_status.name == "COMPLETED"


def workflow_output(run_id: str) -> dict[str, Any]:
    client = DaprWorkflowClient(host="127.0.0.1", port=str(GRPC_PORT))
    try:
        state = client.get_workflow_state(run_id)
    finally:
        cast(Any, client).close()
    assert state is not None
    output = json.loads(state.serialized_output)
    assert isinstance(output, dict)
    return cast(dict[str, Any], output)


def effect(run_id: str, node_id: str) -> dict[str, Any] | None:
    identity = derive_node_execution_id(run_id, node_id)
    value = state_value(f"flow-fake-effect:{identity}")
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def catalog_run_ids() -> set[str]:
    value = state_value("flow-workflow-index")
    if value is None:
        return set()
    assert isinstance(value, list)
    return {
        item["runId"]
        for item in value
        if isinstance(item, dict) and isinstance(item.get("runId"), str)
    }


def state_value(key: str) -> Any:
    url = f"http://127.0.0.1:{HTTP_PORT}/v1.0/state/flowstatestore/{quote(key, safe='')}"
    with urlopen(url, timeout=5) as response:
        data = response.read()
    return json.loads(data) if data else None


def assert_succeeded_fan_in(status: dict[str, Any], run_id: str) -> None:
    assert status["runId"] == run_id
    assert status["state"] == "succeeded"
    assert all(status["nodes"][node]["state"] == "succeeded" for node in ("A", "B", "C"))
    transitions = [
        (event.get("type"), event.get("nodeId"))
        for event in status["history"]
        if event.get("type") in {"dispatch", "node_succeeded"}
    ]
    dispatch_c = transitions.index(("dispatch", "C"))
    assert transitions.index(("node_succeeded", "A")) < dispatch_c
    assert transitions.index(("node_succeeded", "B")) < dispatch_c
    output = workflow_output(run_id)
    assert output["nodes"]["C"]["output"] == {"values": ["A", "B"]}


def wait_until(predicate: Any, label: str, *, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError(f"{label} did not become ready")


def stop_worker(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
    subprocess.run(
        ("dapr", "stop", "--app-id", APP_ID),
        capture_output=True,
        text=True,
        check=False,
    )


def crash_worker(process: subprocess.Popen[bytes]) -> None:
    for pid in registered_process_ids():
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    if process.poll() is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
    subprocess.run(
        ("dapr", "stop", "--app-id", APP_ID),
        capture_output=True,
        text=True,
        check=False,
    )
    wait_until(lambda: readiness()["sidecar"] is False, "Dapr sidecar shutdown", timeout=20)


def registered_process_ids() -> tuple[int, ...]:
    completed = subprocess.run(
        ("dapr", "list", "--output", "json"),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError("Dapr application inventory is unavailable")
    applications = json.loads(completed.stdout)
    application = next(
        (item for item in applications if isinstance(item, dict) and item.get("appId") == APP_ID),
        {},
    )
    return tuple(
        pid
        for name in ("appPid", "daprdPid")
        if isinstance((pid := application.get(name)), int) and pid > 0
    )


def require_clean_dapr_app() -> None:
    completed = subprocess.run(
        ("dapr", "list", "--output", "json"),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError("Dapr application inventory is unavailable")
    try:
        applications = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError("Dapr application inventory is invalid") from error
    if any(item.get("appId") == APP_ID for item in applications if isinstance(item, dict)):
        raise AssertionError(f"Dapr application {APP_ID!r} is already running")


def required_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise AssertionError(f"required executable {name!r} is unavailable")
    return executable


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_fixture(name: str) -> dict[str, Any]:
    value = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)
