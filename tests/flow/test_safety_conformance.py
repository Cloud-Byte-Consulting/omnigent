import ast
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from dapr.clients.exceptions import DaprInternalError
from dapr.ext.workflow.workflow_state import WorkflowStatus

from omnigent.flow.approval import ApprovalRecord, ApprovalService, InMemoryApprovalStore
from omnigent.flow.audit import DaprAuditStore, create_audit_event
from omnigent.flow.caps import CapProposal, DaprCapStore
from omnigent.flow.contracts import RunCaps
from omnigent.flow.providers import TokenUsage
from omnigent.flow.status import WorkflowStatusService
from omnigent.flow.usage import ConservativeUsagePolicy, DaprUsageStore, UsageService

NOW = datetime(2026, 7, 21, tzinfo=UTC)
MANIFEST = Path(__file__).parent / "fixtures" / "safety" / "manifest.json"
REQUIRED_FIXTURES = {
    "valid-approval",
    "stale-approval",
    "duplicate-confirmation",
    "node-cap",
    "round-cap",
    "token-cap",
    "concurrency-throttling",
    "pause-resume-cancel",
    "failed-node",
    "accepted-rejected-expansion",
    "replayed-usage-audit",
    "unknown-run",
    "redacted-sensitive-data",
}


@dataclass
class StateResponse:
    data: bytes
    etag: str


class SharedDaprStateClient:
    """One optimistic-concurrency boundary shared by all durable safety stores."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], tuple[bytes, int]] = {}

    def get_state(self, store_name: str, key: str) -> StateResponse:
        value, version = self.values.get((store_name, key), (b"", 0))
        return StateResponse(value, str(version) if version else "")

    def save_state(self, store_name, key, value, *, etag, options):
        del options
        _, version = self.values.get((store_name, key), (b"", 0))
        expected = str(version) if version else None
        if etag != expected:
            raise DaprInternalError("etag mismatch")
        encoded = value.encode() if isinstance(value, str) else value
        self.values[(store_name, key)] = (encoded, version + 1)


class RecordingStarter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, run_id: str, approval: ApprovalRecord) -> None:
        self.calls.append((run_id, approval.approval_id))


@dataclass
class WorkflowState:
    runtime_status: WorkflowStatus
    created_at: datetime
    last_updated_at: datetime
    serialized_input: str
    serialized_output: str | None
    serialized_custom_status: str | None


class WorkflowClient:
    def __init__(self, state: WorkflowState | None) -> None:
        self.state = state

    def get_workflow_state(self, instance_id: str) -> WorkflowState | None:
        return self.state if instance_id == "run-1" else None


def _dag(*, instructions: str = "Run A") -> dict[str, Any]:
    return {
        "version": "1.0",
        "defaultModel": "fake:deterministic",
        "nodes": [{"id": "A", "instructions": instructions}],
        "caps": {
            "maxNodes": 1,
            "maxRounds": 1,
            "maxConcurrent": 1,
            "tokenBudget": 20,
        },
    }


def _workflow_state() -> WorkflowState:
    workflow_input = {
        "runId": "run-1",
        "approvedDagDigest": "approved-digest",
        "dagSpec": _dag(),
        "persistedResults": {},
    }
    result = {
        "status": "succeeded",
        "nodes": {"A": {"status": "succeeded", "output": {"answer": 42}}},
        "events": [],
    }
    return WorkflowState(
        runtime_status=WorkflowStatus.COMPLETED,
        created_at=NOW,
        last_updated_at=NOW + timedelta(seconds=2),
        serialized_input=json.dumps(workflow_input),
        serialized_output=json.dumps(result),
        serialized_custom_status=json.dumps(result),
    )


@pytest.mark.parametrize(
    "candidate",
    [
        _dag(instructions="Changed after approval"),
        {**_dag(), "nodes": []},
    ],
    ids=("changed-revision", "invalid-revision"),
)
def test_stale_approval_denial_is_ordered_and_never_dispatches(
    candidate: dict[str, Any],
) -> None:
    state_client = SharedDaprStateClient()
    audit = DaprAuditStore(state_client)
    starter = RecordingStarter()
    ids = iter(("approval-1", "run-unused"))
    approvals = ApprovalService(
        InMemoryApprovalStore(),
        signing_key=b"safety-fixture-signing-key",
        start_run=starter,
        id_factory=lambda: next(ids),
        audit=audit,
    )
    preview = approvals.preview(_dag())
    token = approvals.record_decision(
        preview,
        approver="operator",
        decision="approved",
        decided_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )

    result = approvals.confirm(token, candidate, now=NOW)

    assert result.error == "approval_invalid"
    assert starter.calls == []
    history = audit.history("approval-1")
    assert [(event.sequence, event.type) for event in history] == [(1, "denial")]
    assert history[0].metadata == {
        "approvalId": "approval-1",
        "approver": "operator",
        "decision": "approved",
        "reason": "approval_invalid",
    }


def test_duplicate_confirmation_starts_and_audits_once() -> None:
    state_client = SharedDaprStateClient()
    audit = DaprAuditStore(state_client)
    starter = RecordingStarter()
    ids = iter(("approval-1", "run-1", "unused"))
    approvals = ApprovalService(
        InMemoryApprovalStore(),
        signing_key=b"safety-fixture-signing-key",
        start_run=starter,
        id_factory=lambda: next(ids),
        audit=audit,
    )
    preview = approvals.preview(_dag())
    token = approvals.record_decision(
        preview,
        approver="operator",
        decision="approved",
        decided_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )

    first = approvals.confirm(token, _dag(), now=NOW)
    replay = approvals.confirm(token, _dag(), now=NOW + timedelta(seconds=1))

    assert first.run_id == replay.run_id == "run-1"
    assert (first.reused, replay.reused) == (False, True)
    assert starter.calls == [("run-1", "approval-1")]
    history = audit.history("run-1")
    assert [(event.sequence, event.type) for event in history] == [(1, "approval")]
    assert "token" not in json.dumps(history[0].to_dict()).lower()


def test_replayed_usage_and_audit_do_not_double_composed_status() -> None:
    state_client = SharedDaprStateClient()
    audit = DaprAuditStore(state_client)
    usage = UsageService(
        DaprUsageStore(state_client),
        missing_usage_policy=ConservativeUsagePolicy(5),
    )
    caps = DaprCapStore(state_client)
    parsed_limits = RunCaps.model_validate(_dag()["caps"])
    caps.apply(
        "run-1",
        parsed_limits,
        CapProposal.accept_nodes("round-1", ("A",), round_number=1),
        used_tokens=0,
    )
    event = create_audit_event(
        run_id="run-1",
        node_id="A",
        event_type="node_succeeded",
        timestamp=NOW,
        source="provider",
        correlation_key="stable-A:succeeded",
        summary="Node succeeded with sk-sensitive-value",
        metadata={"attempt": 1, "authorization": "Bearer hidden"},
    )
    audit.append(event)
    audit.append(event)
    usage_values = {
        "run_id": "run-1",
        "idempotency_key": "stable-A:attempt:1",
        "node_id": "A",
        "attempt": 1,
        "provider": "fake",
        "model": "deterministic",
        "succeeded": True,
        "usage": TokenUsage(input_tokens=6, output_tokens=4, total_tokens=10),
        "token_budget": 20,
    }
    usage.record_attempt(**usage_values)
    usage.record_attempt(**usage_values)

    status = WorkflowStatusService(
        WorkflowClient(_workflow_state()),
        audit=DaprAuditStore(state_client),
        usage=UsageService(
            DaprUsageStore(state_client),
            missing_usage_policy=ConservativeUsagePolicy(5),
        ),
        caps=DaprCapStore(state_client),
        authorizer=lambda actor, run_id: actor == "operator" and run_id == "run-1",
    ).get("run-1", actor="operator")

    assert status["state"] == "succeeded"
    assert status["caps"]["utilization"]["usedTokens"] == 10
    assert status["nodes"]["A"]["usage"]["totalTokens"] == 10
    assert len(status["history"]) == 1
    serialized = json.dumps(status)
    assert "sk-sensitive-value" not in serialized
    assert "Bearer hidden" not in serialized
    assert "[REDACTED]" in serialized


def test_safety_manifest_maps_every_required_fixture_to_a_real_named_test() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["schemaVersion"] == "1.0"
    assert {fixture["id"] for fixture in manifest["fixtures"]} == REQUIRED_FIXTURES
    assert set(manifest["parentScenarios"]) == {
        "require-human-approval",
        "enforce-run-caps",
        "control-active-runs",
        "return-auditable-status",
    }
    mapped = [
        fixture_id
        for fixture_ids in manifest["parentScenarios"].values()
        for fixture_id in fixture_ids
    ]
    assert set(mapped) == REQUIRED_FIXTURES
    assert len(mapped) == len(REQUIRED_FIXTURES)
    for fixture in manifest["fixtures"]:
        assert fixture["level"] in {"unit", "integration", "end-to-end"}
        path = Path(__file__).parents[2] / fixture["file"]
        functions = {
            node.name
            for node in ast.parse(path.read_text(encoding="utf-8")).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert fixture["test"] in functions
