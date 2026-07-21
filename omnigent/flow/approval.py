"""Revision-bound human approval for Flow workflows."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Protocol, TypeAlias, cast

from omnigent.flow.audit import AuditEvent, create_audit_event
from omnigent.flow.contracts import DagSpec
from omnigent.flow.validation import validate_dag

APPROVAL_INVALID: Literal["approval_invalid"] = "approval_invalid"
ApprovalDecision: TypeAlias = Literal["approved", "denied", "canceled"]
ModelToolSnapshot: TypeAlias = tuple[tuple[str, str, tuple[str, ...]], ...]
JsonObject: TypeAlias = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ApprovalPreview:
    """Complete, non-dispatching view shown to an authorized reviewer."""

    digest: str
    contract_version: str
    dag: JsonObject
    caps_snapshot: dict[str, int]
    model_tool_snapshot: ModelToolSnapshot
    validation_warnings: tuple[str, ...]
    usage_estimate: Literal["unknown"] = "unknown"


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """Persisted human decision bound to one exact workflow revision."""

    approval_id: str
    approver: str
    decision: ApprovalDecision
    decided_at: datetime
    dag_digest: str
    dag_snapshot: JsonObject
    caps_snapshot: dict[str, int]
    model_tool_snapshot: ModelToolSnapshot
    expires_at: datetime
    token_hash: str
    idempotency_key: str | None = None
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    """Result of confirming a signed approval token."""

    run_id: str | None
    error: Literal["approval_invalid"] | None
    reused: bool


class ApprovalStore(Protocol):
    """Persistence operations required by the approval service."""

    def put(self, record: ApprovalRecord) -> None: ...

    def get(self, approval_id: str) -> ApprovalRecord: ...

    def start_once(
        self,
        approval_id: str,
        run_id: str,
        start_run: Callable[[str, ApprovalRecord], None],
    ) -> tuple[str, bool]: ...


class ApprovalAuditStore(Protocol):
    """Optional durable audit boundary for confirmation outcomes."""

    def append_many(self, events: Sequence[AuditEvent]) -> tuple[AuditEvent, ...]: ...


class InMemoryApprovalStore:
    """Thread-safe approval store for tests and embedded use."""

    def __init__(self) -> None:
        self._records: dict[str, ApprovalRecord] = {}
        self._lock = Lock()

    def put(self, record: ApprovalRecord) -> None:
        with self._lock:
            if record.approval_id in self._records:
                raise ValueError(f"duplicate approval ID {record.approval_id}")
            self._records[record.approval_id] = replace(
                record,
                dag_snapshot=_json_copy(record.dag_snapshot),
                caps_snapshot=dict(record.caps_snapshot),
            )

    def get(self, approval_id: str) -> ApprovalRecord:
        with self._lock:
            record = self._records[approval_id]
            return replace(
                record,
                dag_snapshot=_json_copy(record.dag_snapshot),
                caps_snapshot=dict(record.caps_snapshot),
            )

    def start_once(
        self,
        approval_id: str,
        run_id: str,
        start_run: Callable[[str, ApprovalRecord], None],
    ) -> tuple[str, bool]:
        with self._lock:
            record = self._records[approval_id]
            if record.run_id is not None:
                return record.run_id, False
            start_run(run_id, record)
            self._records[approval_id] = replace(record, run_id=run_id)
            return run_id, True


class SQLiteApprovalStore:
    """Small durable SQLite implementation of the approval boundary."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS flow_approvals (
                    approval_id TEXT PRIMARY KEY,
                    approver TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    dag_digest TEXT NOT NULL,
                    dag_snapshot TEXT NOT NULL,
                    caps_snapshot TEXT NOT NULL,
                    model_tool_snapshot TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    idempotency_key TEXT,
                    run_id TEXT
                )
                """
            )
            columns = {
                cast(str, row[1])
                for row in connection.execute("PRAGMA table_info(flow_approvals)").fetchall()
            }
            if "dag_snapshot" not in columns:
                connection.execute(
                    "ALTER TABLE flow_approvals ADD COLUMN dag_snapshot TEXT NOT NULL DEFAULT '{}'"
                )
            if "idempotency_key" not in columns:
                connection.execute("ALTER TABLE flow_approvals ADD COLUMN idempotency_key TEXT")

    def put(self, record: ApprovalRecord) -> None:
        values = _record_values(record)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO flow_approvals (
                    approval_id, approver, decision, decided_at, dag_digest,
                    dag_snapshot, caps_snapshot, model_tool_snapshot, expires_at,
                    token_hash, idempotency_key, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )

    def get(self, approval_id: str) -> ApprovalRecord:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT approval_id, approver, decision, decided_at, dag_digest,
                       dag_snapshot, caps_snapshot, model_tool_snapshot, expires_at,
                       token_hash, idempotency_key, run_id
                FROM flow_approvals
                WHERE approval_id = ?
                """,
                (approval_id,),
            ).fetchone()
        if row is None:
            raise KeyError(approval_id)
        return _record_from_row(row)

    def start_once(
        self,
        approval_id: str,
        run_id: str,
        start_run: Callable[[str, ApprovalRecord], None],
    ) -> tuple[str, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT approval_id, approver, decision, decided_at, dag_digest,
                       dag_snapshot, caps_snapshot, model_tool_snapshot, expires_at,
                       token_hash, idempotency_key, run_id
                FROM flow_approvals WHERE approval_id = ?
                """,
                (approval_id,),
            ).fetchone()
            if row is None:
                raise KeyError(approval_id)
            record = _record_from_row(row)
            if record.run_id is not None:
                return record.run_id, False
            start_run(run_id, record)
            connection.execute(
                "UPDATE flow_approvals SET run_id = ? WHERE approval_id = ?",
                (run_id, approval_id),
            )
            return run_id, True

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)


class ApprovalService:
    """Create previews, record decisions, and confirm exact revisions."""

    def __init__(
        self,
        store: ApprovalStore,
        *,
        signing_key: bytes,
        start_run: Callable[[str, ApprovalRecord], None],
        id_factory: Callable[[], str],
        audit: ApprovalAuditStore | None = None,
    ) -> None:
        if len(signing_key) < 16:
            raise ValueError("signing_key must contain at least 16 bytes")
        self._store = store
        self._signing_key = signing_key
        self._start_run = start_run
        self._id_factory = id_factory
        self._audit = audit

    def preview(
        self,
        value: DagSpec | Mapping[str, Any],
        *,
        validation_warnings: Sequence[str] = (),
    ) -> ApprovalPreview:
        """Return a complete approval view without dispatching work."""
        result = validate_dag(value)
        if result.dag is None:
            codes = ", ".join(error.code for error in result.errors)
            raise ValueError(f"workflow is invalid: {codes}")
        dag = result.dag.model_dump(mode="json", by_alias=True)
        return ApprovalPreview(
            digest=_dag_digest(dag),
            contract_version=result.dag.version,
            dag=dag,
            caps_snapshot=_caps_snapshot(result.dag),
            model_tool_snapshot=_model_tool_snapshot(result.dag),
            validation_warnings=tuple(validation_warnings),
        )

    def record_decision(
        self,
        preview: ApprovalPreview,
        *,
        approver: str,
        decision: ApprovalDecision,
        decided_at: datetime,
        expires_at: datetime,
        idempotency_key: str | None = None,
    ) -> str:
        """Persist a decision and return its signed, revision-bound token."""
        if not approver:
            raise ValueError("approver is required")
        _require_aware(decided_at)
        _require_aware(expires_at)
        if expires_at <= decided_at:
            raise ValueError("approval expiry must be after the decision")
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency_key cannot be blank")
        current_preview = self.preview(
            preview.dag,
            validation_warnings=preview.validation_warnings,
        )
        if current_preview != preview:
            raise ValueError("approval preview no longer matches its workflow revision")

        approval_id = self._id_factory()
        payload = {
            "approvalId": approval_id,
            "dagDigest": preview.digest,
            "expiresAt": expires_at.astimezone(UTC).isoformat(),
            "idempotencyKey": idempotency_key,
        }
        token = _sign_token(payload, self._signing_key)
        self._store.put(
            ApprovalRecord(
                approval_id=approval_id,
                approver=approver,
                decision=decision,
                decided_at=decided_at,
                dag_digest=preview.digest,
                dag_snapshot=_json_copy(preview.dag),
                caps_snapshot=dict(preview.caps_snapshot),
                model_tool_snapshot=preview.model_tool_snapshot,
                expires_at=expires_at,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                idempotency_key=idempotency_key,
            )
        )
        return token

    def confirm(
        self,
        token: str,
        value: DagSpec | Mapping[str, Any],
        *,
        now: datetime,
        idempotency_key: str | None = None,
    ) -> ConfirmationResult:
        """Start an exact approved revision once, or return approval_invalid."""
        _require_aware(now)
        payload = _verify_token(token, self._signing_key)
        if payload is None:
            return _invalid()
        approval_id = payload.get("approvalId")
        if not isinstance(approval_id, str):
            return _invalid()
        try:
            record = self._store.get(approval_id)
        except KeyError:
            return _invalid()
        try:
            preview = self.preview(value)
        except ValueError:
            self._audit_confirmation(record, record.approval_id, now=now, accepted=False)
            return _invalid()

        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expected_expiry = record.expires_at.astimezone(UTC).isoformat()
        if (
            record.decision != "approved"
            or now >= record.expires_at
            or not hmac.compare_digest(token_hash, record.token_hash)
            or payload.get("dagDigest") != record.dag_digest
            or payload.get("expiresAt") != expected_expiry
            or payload.get("idempotencyKey") != record.idempotency_key
            or idempotency_key != record.idempotency_key
            or preview.digest != record.dag_digest
            or preview.dag != record.dag_snapshot
            or preview.caps_snapshot != record.caps_snapshot
            or preview.model_tool_snapshot != record.model_tool_snapshot
        ):
            self._audit_confirmation(record, record.approval_id, now=now, accepted=False)
            return _invalid()

        candidate_run_id = self._id_factory()

        def start_approved(run_id: str, approved: ApprovalRecord) -> None:
            self._audit_confirmation(approved, run_id, now=now, accepted=True)
            self._start_run(run_id, approved)

        run_id, created = self._store.start_once(
            record.approval_id,
            candidate_run_id,
            start_approved,
        )
        self._audit_confirmation(record, run_id, now=now, accepted=True)
        self._audit_run_queued(record, run_id, now=now)
        return ConfirmationResult(run_id=run_id, error=None, reused=not created)

    def _audit_confirmation(
        self,
        record: ApprovalRecord,
        run_id: str,
        *,
        now: datetime,
        accepted: bool,
    ) -> None:
        if self._audit is None:
            return
        outcome = "approved" if accepted else "denied"
        approval = create_audit_event(
            run_id=run_id,
            node_id=None,
            event_type="approval" if accepted else "denial",
            timestamp=now,
            source="approval_confirmation",
            correlation_key=f"approval:{record.approval_id}:confirmation:{outcome}",
            summary=f"Approval confirmation {outcome}",
            metadata={
                "approvalId": record.approval_id,
                "approver": record.approver,
                "decision": record.decision,
                "reason": None if accepted else APPROVAL_INVALID,
            },
        )
        if not accepted:
            self._audit.append_many((approval,))
            return
        self._audit.append_many(
            (
                create_audit_event(
                    run_id=run_id,
                    node_id=None,
                    event_type="validation",
                    timestamp=now,
                    source="approval_confirmation",
                    correlation_key=f"approval:{record.approval_id}:validation",
                    summary="Approved workflow passed validation",
                    metadata={"valid": True, "dagDigest": record.dag_digest},
                ),
                create_audit_event(
                    run_id=run_id,
                    node_id=None,
                    event_type="preview",
                    timestamp=now,
                    source="approval_confirmation",
                    correlation_key=f"approval:{record.approval_id}:preview",
                    summary="Workflow approval preview was created",
                    metadata={
                        "dagDigest": record.dag_digest,
                        "caps": record.caps_snapshot,
                    },
                ),
                approval,
            )
        )

    def _audit_run_queued(
        self,
        record: ApprovalRecord,
        run_id: str,
        *,
        now: datetime,
    ) -> None:
        if self._audit is None:
            return
        self._audit.append_many(
            (
                create_audit_event(
                    run_id=run_id,
                    node_id=None,
                    event_type="run_queued",
                    timestamp=now,
                    source="approval_confirmation",
                    correlation_key=f"{run_id}:queued",
                    summary="Approved workflow was queued",
                    metadata={"approvalId": record.approval_id},
                ),
            )
        )


def _dag_digest(dag: JsonObject) -> str:
    canonical = json.dumps(dag, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _caps_snapshot(dag: DagSpec) -> dict[str, int]:
    return cast(dict[str, int], dag.caps.model_dump(mode="json", by_alias=True))


def _model_tool_snapshot(dag: DagSpec) -> ModelToolSnapshot:
    return tuple(
        (
            node.id,
            node.model or dag.default_model or "",
            tuple(node.tools or ()),
        )
        for node in dag.nodes
    )


def _sign_token(payload: JsonObject, key: bytes) -> str:
    encoded = _base64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signature = _base64(hmac.digest(key, encoded.encode(), "sha256"))
    return f"{encoded}.{signature}"


def _verify_token(token: str, key: bytes) -> JsonObject | None:
    try:
        encoded, supplied_signature = token.split(".", maxsplit=1)
        expected_signature = _base64(hmac.digest(key, encoded.encode(), "sha256"))
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        value = json.loads(_unbase64(encoded))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _unbase64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _invalid() -> ConfirmationResult:
    return ConfirmationResult(run_id=None, error=APPROVAL_INVALID, reused=False)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")


def _record_values(record: ApprovalRecord) -> tuple[object, ...]:
    return (
        record.approval_id,
        record.approver,
        record.decision,
        record.decided_at.isoformat(),
        record.dag_digest,
        json.dumps(record.dag_snapshot, sort_keys=True, separators=(",", ":")),
        json.dumps(record.caps_snapshot, sort_keys=True, separators=(",", ":")),
        json.dumps(record.model_tool_snapshot, separators=(",", ":")),
        record.expires_at.isoformat(),
        record.token_hash,
        record.idempotency_key,
        record.run_id,
    )


def _record_from_row(row: Sequence[object]) -> ApprovalRecord:
    dag_snapshot = json.loads(cast(str, row[5]))
    caps = json.loads(cast(str, row[6]))
    model_tools = json.loads(cast(str, row[7]))
    return ApprovalRecord(
        approval_id=cast(str, row[0]),
        approver=cast(str, row[1]),
        decision=cast(ApprovalDecision, row[2]),
        decided_at=datetime.fromisoformat(cast(str, row[3])),
        dag_digest=cast(str, row[4]),
        dag_snapshot=cast(JsonObject, dag_snapshot),
        caps_snapshot=cast(dict[str, int], caps),
        model_tool_snapshot=tuple(
            (cast(str, item[0]), cast(str, item[1]), tuple(cast(list[str], item[2])))
            for item in cast(list[list[object]], model_tools)
        ),
        expires_at=datetime.fromisoformat(cast(str, row[8])),
        token_hash=cast(str, row[9]),
        idempotency_key=cast(str | None, row[10]),
        run_id=cast(str | None, row[11]),
    )


def _json_copy(value: JsonObject) -> JsonObject:
    return cast(JsonObject, json.loads(json.dumps(value, allow_nan=False)))
