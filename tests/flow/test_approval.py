from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnigent.flow.approval import (
    APPROVAL_INVALID,
    ApprovalRecord,
    ApprovalService,
    InMemoryApprovalStore,
)
from omnigent.flow.audit import AuditEvent, InMemoryAuditStore

NOW = datetime(2026, 7, 21, tzinfo=UTC)


def dag(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "version": "1.0",
        "defaultModel": "fake:default",
        "nodes": [
            {
                "id": "A",
                "instructions": "Inspect the private customer request",
                "tools": ["search"],
                "outputSchema": {"type": "object"},
            },
            {
                "id": "B",
                "instructions": "Summarize A",
                "dependsOn": ["A"],
                "model": "fake:summarizer",
            },
        ],
        "caps": {
            "maxNodes": 4,
            "maxRounds": 2,
            "maxConcurrent": 2,
            "tokenBudget": 100,
        },
    }
    value.update(changes)
    return value


class RecordingStarter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, run_id: str, approval: ApprovalRecord) -> None:
        self.calls.append((run_id, approval.approval_id))


def service(
    starter: RecordingStarter,
    store: InMemoryApprovalStore | None = None,
    audit: InMemoryAuditStore | None = None,
) -> ApprovalService:
    ids = iter(("approval-1", "run-1", "run-2"))
    return ApprovalService(
        store or InMemoryApprovalStore(),
        signing_key=b"test-signing-key",
        start_run=starter,
        id_factory=lambda: next(ids),
        audit=audit,
    )


def test_preview_is_complete_deterministic_and_never_dispatches() -> None:
    starter = RecordingStarter()
    approvals = service(starter)

    first = approvals.preview(dag())
    second = approvals.preview(dag())

    assert first == second
    assert len(first.digest) == 64
    assert first.contract_version == "1.0"
    assert first.dag["nodes"][0]["instructions"] == "Inspect the private customer request"
    assert first.caps_snapshot == dag()["caps"]
    assert first.model_tool_snapshot == (
        ("A", "fake:default", ("search",)),
        ("B", "fake:summarizer", ()),
    )
    assert first.validation_warnings == ()
    assert first.usage_estimate == "unknown"
    assert starter.calls == []


def test_unchanged_approval_starts_once_and_retry_returns_existing_run() -> None:
    starter = RecordingStarter()
    store = InMemoryApprovalStore()
    approvals = service(starter, store)
    preview = approvals.preview(dag())
    token = approvals.record_decision(
        preview,
        approver="reviewer@example.com",
        decision="approved",
        decided_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )

    first = approvals.confirm(token, dag(), now=NOW)
    second = approvals.confirm(token, dag(), now=NOW + timedelta(seconds=1))

    assert first.run_id == "run-1"
    assert first.error is None
    assert first.reused is False
    assert second.run_id == "run-1"
    assert second.error is None
    assert second.reused is True
    assert starter.calls == [("run-1", "approval-1")]
    assert store.get("approval-1").run_id == "run-1"


@pytest.mark.parametrize(
    "condition",
    ["expired", "different-digest", "different-caps", "different-tools", "forged", "denied"],
)
def test_invalid_approval_never_starts_run(condition: str) -> None:
    starter = RecordingStarter()
    approvals = service(starter)
    preview = approvals.preview(dag())
    decision = "denied" if condition == "denied" else "approved"
    token = approvals.record_decision(
        preview,
        approver="reviewer@example.com",
        decision=decision,
        decided_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )
    candidate = dag()
    now = NOW
    if condition == "expired":
        now = NOW + timedelta(minutes=11)
    elif condition == "different-digest":
        candidate["nodes"][0]["instructions"] = "Changed instructions"
    elif condition == "different-caps":
        candidate["caps"]["maxConcurrent"] = 1
    elif condition == "different-tools":
        candidate["nodes"][0]["tools"] = []
    elif condition == "forged":
        token = f"{token[:-1]}A"

    result = approvals.confirm(token, candidate, now=now)

    assert result.run_id is None
    assert result.error == APPROVAL_INVALID
    assert result.reused is False
    assert starter.calls == []


def test_recorded_decision_contains_required_safe_snapshot() -> None:
    starter = RecordingStarter()
    store = InMemoryApprovalStore()
    approvals = service(starter, store)
    preview = approvals.preview(dag())
    token = approvals.record_decision(
        preview,
        approver="reviewer@example.com",
        decision="canceled",
        decided_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )

    record = store.get("approval-1")

    assert record.approver == "reviewer@example.com"
    assert record.decision == "canceled"
    assert record.decided_at == NOW
    assert record.dag_digest == preview.digest
    assert record.caps_snapshot == preview.caps_snapshot
    assert record.model_tool_snapshot == preview.model_tool_snapshot
    assert record.expires_at == NOW + timedelta(minutes=10)
    assert record.token_hash
    assert token not in repr(record)
    assert "resume" not in repr(record).lower()


def test_materially_changed_stored_snapshot_is_rejected() -> None:
    class TamperingStore(InMemoryApprovalStore):
        def get(self, approval_id: str):
            record = super().get(approval_id)
            return replace(
                record,
                caps_snapshot={**record.caps_snapshot, "maxNodes": 99},
            )

    starter = RecordingStarter()
    store = TamperingStore()
    approvals = service(starter, store)
    preview = approvals.preview(dag())
    token = approvals.record_decision(
        preview,
        approver="reviewer@example.com",
        decision="approved",
        decided_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )
    result = approvals.confirm(token, dag(), now=NOW)

    assert result.error == APPROVAL_INVALID
    assert starter.calls == []


def test_failed_run_start_is_not_mistaken_for_a_completed_confirmation() -> None:
    class FailOnceStarter(RecordingStarter):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def __call__(self, run_id: str, approval: ApprovalRecord) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("starter unavailable")
            super().__call__(run_id, approval)

    starter = FailOnceStarter()
    audit = InMemoryAuditStore()
    approvals = service(starter, audit=audit)
    preview = approvals.preview(dag())
    token = approvals.record_decision(
        preview,
        approver="reviewer@example.com",
        decision="approved",
        decided_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )

    with pytest.raises(RuntimeError, match="starter unavailable"):
        approvals.confirm(token, dag(), now=NOW)
    assert [event.type for event in audit.history("run-1")] == [
        "validation",
        "preview",
        "approval",
    ]
    assert "run_queued" not in [event.type for event in audit.history("run-1")]
    retried = approvals.confirm(token, dag(), now=NOW)

    assert retried.run_id == "run-2"
    assert retried.reused is False
    assert starter.calls == [("run-2", "approval-1")]
    assert [event.type for event in audit.history("run-2")] == [
        "validation",
        "preview",
        "approval",
        "run_queued",
    ]


def test_failed_queued_audit_reuses_durably_bound_run_without_rescheduling() -> None:
    class FailQueuedAuditOnce(InMemoryAuditStore):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def append_many(self, events: Sequence[AuditEvent]) -> tuple[AuditEvent, ...]:
            self.calls += 1
            if self.calls == 3:
                raise RuntimeError("audit unavailable")
            return super().append_many(events)

    starter = RecordingStarter()
    audit = FailQueuedAuditOnce()
    approvals = service(starter, audit=audit)
    preview = approvals.preview(dag())
    token = approvals.record_decision(
        preview,
        approver="reviewer@example.com",
        decision="approved",
        decided_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )

    with pytest.raises(RuntimeError, match="audit unavailable"):
        approvals.confirm(token, dag(), now=NOW)
    retried = approvals.confirm(token, dag(), now=NOW)

    assert retried.run_id == "run-1"
    assert retried.reused is True
    assert starter.calls == [("run-1", "approval-1")]
    assert [event.type for event in audit.history("run-1")] == [
        "validation",
        "preview",
        "approval",
        "run_queued",
    ]
