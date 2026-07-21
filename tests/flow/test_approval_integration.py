import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnigent.flow.approval import ApprovalRecord, ApprovalService, SQLiteApprovalStore

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

    def __call__(self, run_id: str, approval: ApprovalRecord) -> None:
        self.calls.append((run_id, approval.approval_id))


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


def test_sqlite_boundary_migrates_legacy_approval_schema_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "legacy-approvals.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE flow_approvals (
                approval_id TEXT PRIMARY KEY,
                approver TEXT NOT NULL,
                decision TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                dag_digest TEXT NOT NULL,
                caps_snapshot TEXT NOT NULL,
                model_tool_snapshot TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                run_id TEXT
            )
            """
        )

    store = SQLiteApprovalStore(database)
    with sqlite3.connect(database) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(flow_approvals)").fetchall()
        }

    assert {"dag_snapshot", "idempotency_key"} <= columns
    assert store is not None
