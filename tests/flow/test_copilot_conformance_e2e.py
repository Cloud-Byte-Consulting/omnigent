"""Opt-in native GitHub Copilot conformance against the installed Flow wheel."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from omnigent.flow.local_dapr import (
    GRPC_PORT,
    HTTP_PORT,
    check_prerequisites,
    readiness,
)
from tests.flow.native_conformance import (
    assert_succeeded_fan_in as _assert_succeeded_fan_in,
)
from tests.flow.native_conformance import (
    build_wheel as _build_wheel,
)
from tests.flow.native_conformance import (
    catalog_run_ids as _catalog_run_ids,
)
from tests.flow.native_conformance import (
    exercise_expansion_scenario as _exercise_expansion_scenario,
)
from tests.flow.native_conformance import (
    exercise_recovery_scenario as _exercise_recovery_scenario,
)
from tests.flow.native_conformance import (
    exercise_safety_and_provider_scenarios as _exercise_safety_and_provider_scenarios,
)
from tests.flow.native_conformance import (
    install_wheel as _install_wheel,
)
from tests.flow.native_conformance import (
    load_fixture as _load_fixture,
)
from tests.flow.native_conformance import (
    require_clean_dapr_app as _require_clean_dapr_app,
)
from tests.flow.native_conformance import (
    required_executable as _required_executable,
)
from tests.flow.native_conformance import (
    restart_worker as _restart_worker,
)
from tests.flow.native_conformance import (
    run_process_group as _run_process_group,
)
from tests.flow.native_conformance import (
    sha256 as _sha256,
)
from tests.flow.native_conformance import (
    start_installed_worker as _start_installed_worker,
)
from tests.flow.native_conformance import (
    stop_worker as _stop_worker,
)
from tests.flow.native_conformance import (
    wait_for_completion as _wait_for_completion,
)
from tests.flow.native_conformance import (
    wait_until as _wait_until,
)
from tests.flow.native_conformance import (
    worker_environment as _worker_environment,
)
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
            _copilot_call,
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
        expansion_executions, expansion_run_id, _expansion_denied_run_id = (
            _exercise_expansion_scenario(
                _copilot_call,
                copilot,
                config,
                cwd=tmp_path,
                env=copilot_environment,
            )
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
            _copilot_call,
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
