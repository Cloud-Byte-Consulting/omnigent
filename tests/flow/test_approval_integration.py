from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnigent.flow.approval import ApprovalService, SQLiteApprovalStore

NOW = datetime(2026, 7, 21, tzinfo=UTC)
DAG = {
    "version": "1.0",
    "defaultModel": "fake:test",
    "nodes": [{"id": "A", "instructions": "Run A"}],
    "caps": {
        "maxNodes": 2,
        "maxRounds": 2,
        "maxConcurrent": 1,
        "tokenBudget": 100,
    },
}


class RecordingStarter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, run_id: str, approval_id: str) -> None:
        self.calls.append((run_id, approval_id))


def build_service(
    path: Path,
    starter: RecordingStarter,
    ids: Iterator[str],
) -> ApprovalService:
    return ApprovalService(
        SQLiteApprovalStore(path),
        signing_key=b"persistent-test-key",
        start_run=starter,
        id_factory=lambda: next(ids),
    )


def test_sqlite_boundary_persists_approval_and_idempotent_confirmation(tmp_path: Path) -> None:
    database = tmp_path / "approvals.sqlite3"
    starter = RecordingStarter()
    ids = iter(("approval-1", "run-1", "unused"))
    first_process = build_service(database, starter, ids)
    preview = first_process.preview(DAG)
    token = first_process.record_decision(
        preview,
        approver="reviewer@example.com",
        decision="approved",
        decided_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )

    first = first_process.confirm(token, DAG, now=NOW)
    restarted_process = build_service(database, starter, ids)
    repeated = restarted_process.confirm(token, DAG, now=NOW + timedelta(seconds=1))

    assert first.run_id == "run-1"
    assert first.reused is False
    assert repeated.run_id == "run-1"
    assert repeated.reused is True
    assert starter.calls == [("run-1", "approval-1")]
