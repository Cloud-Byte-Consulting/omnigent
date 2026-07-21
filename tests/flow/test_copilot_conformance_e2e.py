"""Opt-in native GitHub Copilot conformance against the installed Flow wheel."""

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
from tempfile import TemporaryDirectory
from typing import Any, cast
from urllib.parse import quote
from urllib.request import urlopen

import pytest
from dapr.ext.workflow import DaprWorkflowClient

from omnigent.flow.local_dapr import (
    APP_ID,
    GRPC_PORT,
    HTTP_PORT,
    check_prerequisites,
    readiness,
    start_command,
)
from omnigent.flow.orchestration import derive_node_execution_id
from tests.flow.native_harness import (
    FLOW_TOOLS,
    CopilotProtocolError,
    CopilotToolExecution,
    build_copilot_command,
    parse_copilot_tool_execution,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "conformance"
SOURCE_DATE_EPOCH = "1767225600"

copilot_gate = pytest.mark.skipif(
    os.environ.get("FLOW_COPILOT_E2E") != "1",
    reason="set FLOW_COPILOT_E2E=1 to run the billable GitHub Copilot gate",
)


@copilot_gate
@pytest.mark.timeout(900)
def test_copilot_completes_installed_flow_workflow_without_leaking_secrets(
    tmp_path: Path,
) -> None:
    """Drive the production MCP boundary through a real GitHub Copilot session."""
    repo = Path(__file__).parents[2]
    copilot = _required_executable("copilot")
    _required_executable("dapr")
    _required_executable("docker")
    _required_executable("gh")
    _required_executable("uv")
    check_prerequisites()
    _require_clean_dapr_app()
    github_token = _github_token()

    wheel = _build_wheel(repo, tmp_path / "distribution-a")
    wheel_digest = _sha256(wheel)
    second_wheel = _build_wheel(repo, tmp_path / "distribution-b")
    assert _sha256(second_wheel) == wheel_digest
    installed = tmp_path / "installed"
    _install_wheel(wheel, installed)

    signing_key = secrets.token_urlsafe(32)
    approval_database = tmp_path / "approvals.sqlite3"
    flow_environment = {
        "FLOW_MODE": "conformance",
        "FLOW_ACTOR": "copilot-e2e-operator",
        "FLOW_SIGNING_KEY": signing_key,
        "FLOW_APPROVAL_DB": str(approval_database),
        "FLOW_APPROVAL_TTL_SECONDS": "300",
        "FLOW_DAPR_HEALTH_TIMEOUT_SECONDS": "5",
        "DAPR_GRPC_PORT": str(GRPC_PORT),
        "DAPR_HTTP_PORT": str(HTTP_PORT),
        "PYTHONNOUSERSITE": "1",
    }
    config = tmp_path / ".mcp.json"
    _write_copilot_config(repo / ".mcp.json", config)
    executable_path = installed / "bin"
    worker_environment = {
        **flow_environment,
        "PATH": f"{executable_path}{os.pathsep}{os.environ['PATH']}",
    }
    worker = _start_installed_worker(repo, tmp_path, installed, worker_environment)
    approval_token: str | None = None
    try:
        _wait_until(lambda: all(readiness().values()), "installed Dapr worker")
        copilot_environment = _copilot_environment(worker_environment, github_token)

        proposal = _copilot_call(
            copilot,
            config,
            "propose_dag",
            {"task_description": "Execute the shared three-node conformance workflow"},
            cwd=tmp_path,
            env=copilot_environment,
        )
        proposed_dag = proposal.structured_result.get("dagSpec")
        assert isinstance(proposed_dag, dict)
        assert [node["id"] for node in proposed_dag["nodes"]] == ["A", "B", "C"]

        fixture = _load_fixture("workflow.json")
        dag = fixture["dagSpec"]
        assert fixture["fixtureRevision"] == "flow-conformance-1.0.0"

        idempotency_key = f"copilot-e2e-{secrets.token_hex(12)}"
        catalog_before_preview = _catalog_run_ids()
        preview = _copilot_call(
            copilot,
            config,
            "run_workflow",
            {
                "dag_spec": dag,
                "confirm": False,
                "idempotency_key": idempotency_key,
            },
            cwd=tmp_path,
            env=copilot_environment,
        )
        assert preview.structured_result["status"] == "approval_required"
        approval_token = preview.structured_result.get("approvalToken")
        assert isinstance(approval_token, str) and approval_token
        assert _catalog_run_ids() == catalog_before_preview

        confirmation = {
            "dag_spec": dag,
            "approval_token": approval_token,
            "confirm": True,
            "idempotency_key": idempotency_key,
        }
        started = _copilot_call(
            copilot,
            config,
            "run_workflow",
            confirmation,
            cwd=tmp_path,
            env=copilot_environment,
        )
        run_id = started.structured_result.get("runId")
        assert isinstance(run_id, str) and run_id
        assert started.structured_result["reused"] is False
        _wait_for_completion(run_id)

        status = _copilot_call(
            copilot,
            config,
            "get_workflow_status",
            {"run_id": run_id},
            cwd=tmp_path,
            env=copilot_environment,
        )
        _assert_succeeded_fan_in(status.structured_result, run_id)

        listed = _copilot_call(
            copilot,
            config,
            "list_workflows",
            {"created_after": started.structured_result["createdAt"], "limit": 100},
            cwd=tmp_path,
            env=copilot_environment,
        )
        listed_run = next(
            item for item in listed.structured_result["workflows"] if item.get("runId") == run_id
        )
        assert listed_run["state"] == "succeeded"

        replayed = _copilot_call(
            copilot,
            config,
            "run_workflow",
            confirmation,
            cwd=tmp_path,
            env=copilot_environment,
        )
        assert replayed.structured_result["runId"] == run_id
        assert replayed.structured_result["reused"] is True

        scenario_executions, provider_run_ids = _exercise_safety_and_provider_scenarios(
            copilot,
            config,
            cwd=tmp_path,
            env=copilot_environment,
        )

        worker = _restart_worker(
            worker,
            repo,
            tmp_path,
            installed,
            {**worker_environment, "FLOW_FAKE_EXPANSION_NODE": "A"},
        )
        expansion_executions, expansion_run_id = _exercise_expansion_scenario(
            copilot,
            config,
            cwd=tmp_path,
            env=copilot_environment,
        )

        worker = _restart_worker(
            worker,
            repo,
            tmp_path,
            installed,
            {
                **worker_environment,
                "FLOW_FAKE_SLOW_NODE": "B",
                "FLOW_FAKE_DELAY_SECONDS": "20",
            },
        )
        recovery_executions, recovery_run_id, worker = _exercise_recovery_scenario(
            copilot,
            config,
            repo=repo,
            cwd=tmp_path,
            installed=installed,
            env=copilot_environment,
            worker_env=worker_environment,
            worker=worker,
        )

        safe_evidence = [
            execution.safe_evidence()
            for execution in (
                proposal,
                preview,
                started,
                status,
                listed,
                replayed,
                *scenario_executions,
                *expansion_executions,
                *recovery_executions,
            )
        ]
        encoded_evidence = json.dumps(safe_evidence, sort_keys=True)
        assert github_token not in encoded_evidence
        assert signing_key not in encoded_evidence
        assert approval_token not in encoded_evidence
        assert "[REDACTED]" in encoded_evidence
        assert {item["tool"] for item in safe_evidence} == set(FLOW_TOOLS)
        print(
            "copilot_e2e_evidence "
            f"run_id={run_id} expansion_run_id={expansion_run_id} "
            f"recovery_run_id={recovery_run_id} wheel_sha256={wheel_digest} "
            f"provider_run_ids={','.join(provider_run_ids)} state=succeeded reused=true"
        )
    finally:
        _stop_worker(worker)


def test_copilot_call_retries_protocol_failure_with_ephemeral_homes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = iter(("not-json", _successful_copilot_output("list_workflows")))
    homes: list[Path] = []

    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del args
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        homes.append(Path(environment["HOME"]))
        return subprocess.CompletedProcess((), 0, next(outputs), "")

    monkeypatch.setattr(
        "tests.flow.test_copilot_conformance_e2e._run_process_group",
        run,
    )

    result = _copilot_call(
        "copilot",
        tmp_path / ".mcp.json",
        "list_workflows",
        {},
        cwd=tmp_path,
        env={},
    )

    assert result.structured_result == {"visibleCount": 0, "workflows": []}
    assert len(homes) == 2
    assert len(set(homes)) == 2
    assert all(not home.exists() for home in homes)


def test_copilot_call_exhausts_retries_without_exposing_sensitive_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        del args, kwargs
        calls += 1
        return subprocess.CompletedProcess(
            (),
            1,
            "opaque-approval-token",
            "Bearer secret-github-token",
        )

    monkeypatch.setattr(
        "tests.flow.test_copilot_conformance_e2e._run_process_group",
        run,
    )

    with pytest.raises(AssertionError) as captured:
        _copilot_call(
            "copilot",
            tmp_path / ".mcp.json",
            "run_workflow",
            {"approval_token": "opaque-approval-token"},
            cwd=tmp_path,
            env={"GH_TOKEN": "secret-github-token"},
        )

    message = str(captured.value)
    assert calls == 3
    assert "opaque-approval-token" not in message
    assert "secret-github-token" not in message
    assert message == (
        "Copilot did not produce the expected run_workflow result after three ephemeral attempts"
    )


def test_worker_environment_drops_ambient_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-be-inherited")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-inherited")

    environment = _worker_environment({"FLOW_MODE": "conformance"})

    assert environment["FLOW_MODE"] == "conformance"
    assert "PATH" in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment


def test_copilot_environment_keeps_only_required_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-be-inherited")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-inherited")
    runtime = {"FLOW_MODE": "conformance", "PATH": os.environ["PATH"]}

    environment = _copilot_environment(runtime, "required-github-token")

    assert environment["GH_TOKEN"] == "required-github-token"
    assert environment["FLOW_MODE"] == "conformance"
    assert environment["CI"] == "1"
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "GITHUB_TOKEN" not in environment


def test_process_group_timeout_kills_descendants(tmp_path: Path) -> None:
    marker = tmp_path / "orphaned"
    script = (
        "import pathlib,time; "
        "time.sleep(0.4); "
        f"pathlib.Path({str(marker)!r}).write_text('orphaned')"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        _run_process_group(
            (_required_executable("python3"), "-c", script),
            cwd=tmp_path,
            env={"PATH": os.environ["PATH"]},
            timeout=0.05,
        )
    time.sleep(0.5)

    assert not marker.exists()


def _exercise_safety_and_provider_scenarios(
    executable: str,
    config: Path,
    *,
    cwd: Path,
    env: dict[str, str],
) -> tuple[list[CopilotToolExecution], list[str]]:
    scenarios = _load_fixture("scenarios.json")
    executions: list[CopilotToolExecution] = []
    before = _catalog_run_ids()
    for case in scenarios["invalidGraphs"]:
        rejected = _copilot_call(
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

    canonical = _load_fixture("workflow.json")["dagSpec"]
    stale_preview = _copilot_call(
        executable,
        config,
        "run_workflow",
        {"dag_spec": canonical, "confirm": False},
        cwd=cwd,
        env=env,
    )
    stale_dag = json.loads(json.dumps(canonical))
    stale_dag["nodes"][0]["instructions"] = "Changed after approval"
    stale = _copilot_call(
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
    assert _catalog_run_ids() == before
    executions.extend((stale_preview, stale))

    substitution = scenarios["providerSubstitution"]
    normalized: list[dict[str, Any]] = []
    provider_run_ids: list[str] = []
    for model in substitution["adapters"]:
        provider_dag = _provider_substitution_dag(model, substitution["expectedNormalizedOutput"])
        provider_preview, provider_started = _preview_and_start(
            executable,
            config,
            provider_dag,
            cwd=cwd,
            env=env,
            idempotency_key=f"copilot-provider-{model}-{secrets.token_hex(12)}",
        )
        provider_run_id = cast(str, provider_started.structured_result["runId"])
        provider_run_ids.append(provider_run_id)
        _wait_for_completion(provider_run_id)
        provider_status = _copilot_call(
            executable,
            config,
            "get_workflow_status",
            {"run_id": provider_run_id},
            cwd=cwd,
            env=env,
        )
        node = provider_status.structured_result["nodes"]["same"]
        output = _workflow_output(provider_run_id)["nodes"]["same"]["output"]
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


def _exercise_expansion_scenario(
    executable: str,
    config: Path,
    *,
    cwd: Path,
    env: dict[str, str],
) -> tuple[list[CopilotToolExecution], str]:
    dag = _load_fixture("scenarios.json")["capAndExpansion"]["baseDag"]
    preview, started = _preview_and_start(
        executable,
        config,
        dag,
        cwd=cwd,
        env=env,
        idempotency_key=f"copilot-expansion-{secrets.token_hex(12)}",
    )
    run_id = cast(str, started.structured_result["runId"])
    _wait_for_completion(run_id)
    status = _copilot_call(
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
        executable,
        config,
        denied_dag,
        cwd=cwd,
        env=env,
        idempotency_key=f"copilot-expansion-denied-{secrets.token_hex(12)}",
    )
    denied_run_id = cast(str, denied_started.structured_result["runId"])
    _wait_for_completion(denied_run_id)
    denied_status = _copilot_call(
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
    return [
        preview,
        started,
        status,
        denied_preview,
        denied_started,
        denied_status,
    ], run_id


def _exercise_recovery_scenario(
    executable: str,
    config: Path,
    *,
    repo: Path,
    cwd: Path,
    installed: Path,
    env: dict[str, str],
    worker_env: dict[str, str],
    worker: subprocess.Popen[bytes],
) -> tuple[list[CopilotToolExecution], str, subprocess.Popen[bytes]]:
    dag = _load_fixture("workflow.json")["dagSpec"]
    preview, started = _preview_and_start(
        executable,
        config,
        dag,
        cwd=cwd,
        env=env,
        idempotency_key=f"copilot-recovery-{secrets.token_hex(12)}",
    )
    run_id = cast(str, started.structured_result["runId"])
    _wait_until(
        lambda: (
            (_effect(run_id, "A") or {}).get("completed") is True
            and (_effect(run_id, "B") or {}).get("completed") is False
        ),
        "recovery checkpoint",
    )
    _crash_worker(worker)
    restarted = _start_installed_worker(repo, cwd, installed, worker_env)
    try:
        _wait_until(lambda: all(readiness().values()), "restarted Dapr worker")
        _wait_for_completion(run_id)
        status = _copilot_call(
            executable,
            config,
            "get_workflow_status",
            {"run_id": run_id},
            cwd=cwd,
            env=env,
        )
        _assert_succeeded_fan_in(status.structured_result, run_id)
        assert all((_effect(run_id, node) or {})["effectCount"] == 1 for node in ("A", "B", "C"))
    except BaseException:
        _stop_worker(restarted)
        raise
    return [preview, started, status], run_id, restarted


def _preview_and_start(
    executable: str,
    config: Path,
    dag: dict[str, Any],
    *,
    cwd: Path,
    env: dict[str, str],
    idempotency_key: str,
) -> tuple[CopilotToolExecution, CopilotToolExecution]:
    preview = _copilot_call(
        executable,
        config,
        "run_workflow",
        {"dag_spec": dag, "confirm": False, "idempotency_key": idempotency_key},
        cwd=cwd,
        env=env,
    )
    assert preview.structured_result["status"] == "approval_required"
    started = _copilot_call(
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


def _copilot_call(
    executable: str,
    config: Path,
    tool: str,
    arguments: dict[str, Any],
    *,
    cwd: Path,
    env: dict[str, str],
) -> CopilotToolExecution:
    __tracebackhide__ = True
    prompt = (
        f"Call the MCP tool flow-{tool} exactly once with exactly this JSON object "
        f"as its arguments: {json.dumps(arguments, sort_keys=True, separators=(',', ':'))}. "
        "Do not call another tool. Stop immediately after the tool result."
    )
    for _attempt in range(3):
        with TemporaryDirectory(prefix="copilot-home-", dir=cwd) as home_value:
            home = Path(home_value)
            command = (
                *build_copilot_command(prompt, executable=executable, mcp_config=config.name),
                "--log-level",
                "none",
                "--log-dir",
                str(home / "logs"),
                "--no-remote",
                "--secret-env-vars=GH_TOKEN,GITHUB_TOKEN",
            )
            process_environment = {
                **env,
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / "config"),
                "XDG_STATE_HOME": str(home / "state"),
            }
            try:
                completed = _run_process_group(
                    command,
                    cwd=cwd,
                    env=process_environment,
                    timeout=180,
                )
            except subprocess.TimeoutExpired:
                continue
            if completed.returncode != 0:
                continue
            try:
                return parse_copilot_tool_execution(completed.stdout, expected_tool=tool)
            except CopilotProtocolError:
                continue
    raise AssertionError(
        f"Copilot did not produce the expected {tool} result after three ephemeral attempts"
    ) from None


def _successful_copilot_output(tool: str) -> str:
    call_id = "call-1"
    return "\n".join(
        (
            json.dumps(
                {
                    "type": "tool.execution_start",
                    "data": {
                        "toolCallId": call_id,
                        "toolName": f"flow-{tool}",
                        "mcpServerName": "flow",
                        "mcpToolName": tool,
                        "arguments": {},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "tool.execution_complete",
                    "data": {
                        "toolCallId": call_id,
                        "success": True,
                        "result": {
                            "structuredContent": {
                                "visibleCount": 0,
                                "workflows": [],
                            }
                        },
                    },
                }
            ),
        )
    )


def _run_process_group(
    command: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _write_copilot_config(source: Path, path: Path) -> None:
    config = json.loads(source.read_text(encoding="utf-8"))
    assert config["mcpServers"]["flow"] == {
        "type": "stdio",
        "command": "flow-mcp",
        "args": [],
        "env": {"FLOW_LOG_LEVEL": "${FLOW_LOG_LEVEL:-INFO}"},
        "tools": list(FLOW_TOOLS),
        "timeout": 120000,
    }
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def _build_wheel(repo: Path, output: Path) -> Path:
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


def _install_wheel(wheel: Path, target: Path) -> None:
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


def _start_installed_worker(
    repo: Path,
    cwd: Path,
    installed: Path,
    flow_environment: dict[str, str],
) -> subprocess.Popen[bytes]:
    environment = _worker_environment(flow_environment)
    return subprocess.Popen(
        start_command(repo, python=str(installed / "bin" / "python")),
        cwd=cwd,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _restart_worker(
    process: subprocess.Popen[bytes],
    repo: Path,
    cwd: Path,
    installed: Path,
    environment: dict[str, str],
) -> subprocess.Popen[bytes]:
    _stop_worker(process)
    restarted = _start_installed_worker(repo, cwd, installed, environment)
    try:
        _wait_until(lambda: all(readiness().values()), "restarted Dapr worker")
    except BaseException:
        _stop_worker(restarted)
        raise
    return restarted


def _worker_environment(flow_environment: dict[str, str]) -> dict[str, str]:
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


def _copilot_environment(runtime: dict[str, str], github_token: str) -> dict[str, str]:
    environment = _worker_environment(runtime)
    environment.update(
        {
            "GH_TOKEN": github_token,
            "CI": "1",
            "NO_COLOR": "1",
        }
    )
    return environment


def _wait_for_completion(run_id: str) -> None:
    client = DaprWorkflowClient(host="127.0.0.1", port=str(GRPC_PORT))
    try:
        completed = client.wait_for_workflow_completion(run_id, timeout_in_seconds=45)
    finally:
        cast(Any, client).close()
    assert completed is not None
    assert completed.runtime_status.name == "COMPLETED"


def _workflow_output(run_id: str) -> dict[str, Any]:
    client = DaprWorkflowClient(host="127.0.0.1", port=str(GRPC_PORT))
    try:
        state = client.get_workflow_state(run_id)
    finally:
        cast(Any, client).close()
    assert state is not None
    output = json.loads(state.serialized_output)
    assert isinstance(output, dict)
    return cast(dict[str, Any], output)


def _effect(run_id: str, node_id: str) -> dict[str, Any] | None:
    identity = derive_node_execution_id(run_id, node_id)
    value = _state_value(f"flow-fake-effect:{identity}")
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _catalog_run_ids() -> set[str]:
    value = _state_value("flow-workflow-index")
    if value is None:
        return set()
    assert isinstance(value, list)
    return {
        item["runId"]
        for item in value
        if isinstance(item, dict) and isinstance(item.get("runId"), str)
    }


def _state_value(key: str) -> Any:
    url = f"http://127.0.0.1:{HTTP_PORT}/v1.0/state/flowstatestore/{quote(key, safe='')}"
    with urlopen(url, timeout=5) as response:
        data = response.read()
    return json.loads(data) if data else None


def _assert_succeeded_fan_in(status: dict[str, Any], run_id: str) -> None:
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
    output = _workflow_output(run_id)
    assert output["nodes"]["C"]["output"] == {"values": ["A", "B"]}


def _wait_until(predicate: Any, label: str, *, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError(f"{label} did not become ready")


def _stop_worker(process: subprocess.Popen[bytes]) -> None:
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


def _crash_worker(process: subprocess.Popen[bytes]) -> None:
    for pid in _registered_process_ids():
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
    _wait_until(lambda: readiness()["sidecar"] is False, "Dapr sidecar shutdown", timeout=20)


def _registered_process_ids() -> tuple[int, ...]:
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


def _require_clean_dapr_app() -> None:
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


def _github_token() -> str:
    completed = subprocess.run(
        ("gh", "auth", "token"),
        capture_output=True,
        text=True,
        check=False,
    )
    token = completed.stdout.strip()
    if completed.returncode != 0 or not token:
        raise AssertionError("GitHub CLI authentication is required for the Copilot gate")
    return token


def _required_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise AssertionError(f"required executable {name!r} is unavailable")
    return executable


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_fixture(name: str) -> dict[str, Any]:
    value = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)
