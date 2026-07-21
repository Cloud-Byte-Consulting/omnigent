import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from omnigent.flow.local_dapr import APP_ID, readiness, start_command

pytestmark = pytest.mark.skipif(
    os.environ.get("FLOW_DAPR_E2E") != "1",
    reason="set FLOW_DAPR_E2E=1 to exercise the destructive local Dapr lifecycle",
)


def _wait_until(predicate, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError("Dapr condition did not become ready")


def _stop(process: subprocess.Popen[str]) -> None:
    process.send_signal(signal.SIGINT)
    process.wait(timeout=15)


def test_workflow_survives_restart_and_supports_operator_lifecycle() -> None:
    os.environ["DAPR_GRPC_PORT"] = "50101"
    os.environ["DAPR_HTTP_PORT"] = "3510"
    from dapr.clients import DaprClient
    from dapr.clients.exceptions import DaprInternalError
    from dapr.ext.workflow import DaprWorkflowClient

    from omnigent.flow.audit import DaprAuditStore, create_audit_event
    from omnigent.flow.lifecycle import LifecycleService

    repo = Path(__file__).parents[2]
    process = subprocess.Popen(
        start_command(repo, python=sys.executable),
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_until(lambda: all(readiness().values()))
        client = DaprWorkflowClient()
        controls = LifecycleService(
            client,
            audit=DaprAuditStore(DaprClient()),
            authorizer=lambda actor, _action, _run_id: actor == "operator",
            clock=lambda: datetime.now(UTC),
        )
        audit_run_id = f"flow-audit-{uuid4()}"
        audit_event = create_audit_event(
            run_id=audit_run_id,
            node_id=None,
            event_type="run_queued",
            timestamp=datetime.now(UTC),
            source="system",
            correlation_key=f"{audit_run_id}:queued",
            summary="Run queued",
            metadata={},
        )
        DaprAuditStore(DaprClient()).append(audit_event)
        instance_id = f"flow-smoke-{uuid4()}"
        client.schedule_new_workflow("FlowRuntimeSmoke", input={}, instance_id=instance_id)
        assert client.wait_for_workflow_start(instance_id, timeout_in_seconds=20)

        assert controls.pause(instance_id, actor="operator").status == "paused"
        _wait_until(
            lambda: client.get_workflow_state(instance_id).runtime_status.name == "SUSPENDED"
        )
        assert controls.resume(instance_id, actor="operator").status == "running"
        _wait_until(
            lambda: client.get_workflow_state(instance_id).runtime_status.name == "RUNNING"
        )
        _stop(process)

        process = subprocess.Popen(
            start_command(repo, python=sys.executable),
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        _wait_until(lambda: all(readiness().values()))
        assert client.get_workflow_state(instance_id).runtime_status.name == "RUNNING"
        restarted_audit = DaprAuditStore(DaprClient())
        assert restarted_audit.history(audit_run_id)[0].event_id == audit_event.event_id
        restarted_audit.append(audit_event)
        assert len(restarted_audit.history(audit_run_id)) == 1
        assert [event.type for event in restarted_audit.history(instance_id)] == [
            "pause",
            "resume",
        ]
        history = subprocess.run(
            ("dapr", "workflow", "history", instance_id, "--app-id", APP_ID),
            check=True,
            capture_output=True,
            text=True,
        )
        assert "FlowRuntimeSmoke" in history.stdout
        assert "RUNNING" in history.stdout

        controls = LifecycleService(
            client,
            audit=restarted_audit,
            authorizer=lambda actor, _action, _run_id: actor == "operator",
            clock=lambda: datetime.now(UTC),
        )
        assert controls.cancel(instance_id, actor="operator").status == "canceled"
        _wait_until(
            lambda: client.get_workflow_state(instance_id).runtime_status.name == "TERMINATED"
        )
        assert [event.type for event in restarted_audit.history(instance_id)] == [
            "pause",
            "resume",
            "cancel",
        ]
        _stop(process)

        subprocess.run(
            (
                sys.executable,
                "-m",
                "omnigent.flow.local_dapr",
                "clean-reset",
                "--yes",
            ),
            cwd=repo,
            check=True,
        )
        process = subprocess.Popen(
            start_command(repo, python=sys.executable),
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        _wait_until(lambda: all(readiness().values()))
        try:
            prior_state = client.get_workflow_state(instance_id)
        except DaprInternalError:
            prior_state = None
        assert prior_state is None
        assert DaprAuditStore(DaprClient()).history(audit_run_id) == ()
    finally:
        if process.poll() is None:
            _stop(process)
