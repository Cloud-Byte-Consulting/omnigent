"""Authorized, filtered, cursor-paginated Flow workflow summaries."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, Protocol, TypeAlias, cast

from dapr.clients.exceptions import DaprInternalError
from dapr.clients.grpc._state import Concurrency, Consistency, StateOptions

RunState: TypeAlias = Literal[
    "pending_approval",
    "queued",
    "running",
    "paused",
    "succeeded",
    "failed",
    "canceled",
    "rejected",
]
RUN_STATES = frozenset(
    {
        "pending_approval",
        "queued",
        "running",
        "paused",
        "succeeded",
        "failed",
        "canceled",
        "rejected",
    }
)
NODE_COUNT_KEYS = (
    "total",
    "blocked",
    "queued",
    "running",
    "succeeded",
    "failed",
    "canceled",
    "skipped",
)
JsonObject = dict[str, Any]


@dataclass(frozen=True, slots=True)
class WorkflowSummary:
    """Internal catalog record; only explicit summary fields are serialized."""

    run_id: str
    dag_digest: str
    dag_name: str | None
    state: RunState
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    node_counts: Mapping[str, int]
    private_detail: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.run_id or not self.dag_digest:
            raise ValueError("run_id and dag_digest are required")
        if self.state not in RUN_STATES:
            raise ValueError("unsupported workflow state")
        for timestamp in (self.created_at, self.updated_at, self.completed_at):
            if timestamp is not None and (
                timestamp.tzinfo is None or timestamp.utcoffset() is None
            ):
                raise ValueError("workflow timestamps must include a timezone")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        counts = dict(self.node_counts)
        if set(counts) != set(NODE_COUNT_KEYS) or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in counts.values()
        ):
            raise ValueError("node_counts must contain non-negative canonical counts")
        if sum(counts[key] for key in NODE_COUNT_KEYS if key != "total") != counts["total"]:
            raise ValueError("node state counts must equal total")
        object.__setattr__(self, "node_counts", counts)
        object.__setattr__(self, "private_detail", dict(self.private_detail))

    def to_dict(self) -> JsonObject:
        return {
            "runId": self.run_id,
            "dagDigest": self.dag_digest,
            "dagName": self.dag_name,
            "state": self.state,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat(),
            "completedAt": self.completed_at.isoformat() if self.completed_at else None,
            "nodeProgress": dict(self.node_counts),
        }


class WorkflowCatalog(Protocol):
    def snapshot(self) -> Sequence[WorkflowSummary]: ...


class InMemoryWorkflowCatalog:
    """Detached stable snapshot catalog for tests and embedded use."""

    def __init__(self, records: Sequence[WorkflowSummary] = ()) -> None:
        self._records = tuple(_copy_summary(record) for record in records)

    def snapshot(self) -> tuple[WorkflowSummary, ...]:
        return tuple(_copy_summary(record) for record in self._records)


class StateResponse(Protocol):
    data: bytes
    etag: str


class DaprStateClient(Protocol):
    def get_state(self, store_name: str, key: str) -> StateResponse: ...

    def save_state(
        self,
        store_name: str,
        key: str,
        value: bytes | str,
        *,
        etag: str | None,
        options: StateOptions,
    ) -> object: ...


class DaprWorkflowCatalog:
    """Durable safe-summary index with optimistic replay-safe updates."""

    def __init__(
        self,
        client: DaprStateClient,
        *,
        store_name: str = "flowstatestore",
        max_attempts: int = 3,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self._client = client
        self._store_name = store_name
        self._max_attempts = max_attempts
        self._options = StateOptions(
            consistency=Consistency.strong,
            concurrency=Concurrency.first_write,
        )

    def upsert(self, record: WorkflowSummary) -> WorkflowSummary:
        """Persist only public summary fields; ignore exact or older replays."""
        last_error: DaprInternalError | None = None
        for _ in range(self._max_attempts):
            response = self._client.get_state(self._store_name, _catalog_key())
            records = list(_decode_catalog(response.data))
            existing = next((item for item in records if item.run_id == record.run_id), None)
            safe_record = _safe_summary(record)
            if existing is not None:
                if existing.to_dict() == safe_record.to_dict():
                    return _copy_summary(existing)
                if existing.updated_at > safe_record.updated_at:
                    return _copy_summary(existing)
                if existing.updated_at == safe_record.updated_at:
                    raise ValueError("conflicting workflow summary has the same updated_at")
            updated = [item for item in records if item.run_id != record.run_id]
            updated.append(safe_record)
            updated.sort(key=lambda item: item.run_id)
            try:
                self._client.save_state(
                    self._store_name,
                    _catalog_key(),
                    json.dumps(
                        [item.to_dict() for item in updated],
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    etag=response.etag or None,
                    options=self._options,
                )
            except DaprInternalError as error:
                last_error = error
                continue
            return _copy_summary(safe_record)
        if last_error is not None:
            raise last_error
        raise RuntimeError("workflow catalog update failed without a Dapr error")

    def snapshot(self) -> tuple[WorkflowSummary, ...]:
        response = self._client.get_state(self._store_name, _catalog_key())
        return tuple(_copy_summary(record) for record in _decode_catalog(response.data))


class WorkflowListingService:
    """Apply authorization and filters before stable summary pagination."""

    def __init__(
        self,
        catalog: WorkflowCatalog,
        *,
        authorizer: Callable[[str, str], bool],
        max_page_size: int = 100,
    ) -> None:
        if max_page_size <= 0:
            raise ValueError("max_page_size must be positive")
        self._catalog = catalog
        self._authorizer = authorizer
        self._max_page_size = max_page_size

    def list(
        self,
        *,
        actor: str,
        state: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        updated_after: str | None = None,
        updated_before: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> JsonObject:
        """Return only authorized summaries or one canonical input error."""
        try:
            filters = _filters(
                state=state,
                created_after=created_after,
                created_before=created_before,
                updated_after=updated_after,
                updated_before=updated_before,
            )
            if not isinstance(limit, int) or isinstance(limit, bool):
                raise ValueError("limit must be an integer")
            if limit <= 0 or limit > self._max_page_size:
                raise ValueError(f"limit must be between 1 and {self._max_page_size}")
            if not actor.strip():
                raise ValueError("actor is required")
        except ValueError as error:
            return _invalid(str(error))

        visible = sorted(
            (
                record
                for record in self._catalog.snapshot()
                if self._authorizer(actor, record.run_id) and _matches(record, filters)
            ),
            key=lambda record: (record.created_at, record.run_id),
        )
        signature = _filter_signature(actor, filters)
        start = 0
        if cursor is not None:
            cursors = [_cursor(record, signature) for record in visible]
            try:
                start = cursors.index(cursor) + 1
            except ValueError:
                return _invalid("cursor is invalid for the authorized filtered snapshot")
        page = visible[start : start + limit]
        has_more = start + len(page) < len(visible)
        return {
            "workflows": [record.to_dict() for record in page],
            "visibleCount": len(visible),
            "nextCursor": _cursor(page[-1], signature) if page and has_more else None,
        }


@dataclass(frozen=True, slots=True)
class _Filters:
    state: RunState | None
    created_after: datetime | None
    created_before: datetime | None
    updated_after: datetime | None
    updated_before: datetime | None


def _filters(
    *,
    state: str | None,
    created_after: str | None,
    created_before: str | None,
    updated_after: str | None,
    updated_before: str | None,
) -> _Filters:
    if state is not None and state not in RUN_STATES:
        raise ValueError("state is invalid")
    result = _Filters(
        state=cast(RunState | None, state),
        created_after=_timestamp(created_after, "created_after"),
        created_before=_timestamp(created_before, "created_before"),
        updated_after=_timestamp(updated_after, "updated_after"),
        updated_before=_timestamp(updated_before, "updated_before"),
    )
    if (
        result.created_after is not None
        and result.created_before is not None
        and result.created_after >= result.created_before
    ):
        raise ValueError("created_after must precede created_before")
    if (
        result.updated_after is not None
        and result.updated_before is not None
        and result.updated_after >= result.updated_before
    ):
        raise ValueError("updated_after must precede updated_before")
    return result


def _timestamp(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed


def _matches(record: WorkflowSummary, filters: _Filters) -> bool:
    return (
        (filters.state is None or record.state == filters.state)
        and (filters.created_after is None or record.created_at > filters.created_after)
        and (filters.created_before is None or record.created_at < filters.created_before)
        and (filters.updated_after is None or record.updated_at > filters.updated_after)
        and (filters.updated_before is None or record.updated_at < filters.updated_before)
    )


def _filter_signature(actor: str, filters: _Filters) -> str:
    value = {
        "actor": actor,
        "state": filters.state,
        "createdAfter": _iso(filters.created_after),
        "createdBefore": _iso(filters.created_before),
        "updatedAfter": _iso(filters.updated_after),
        "updatedBefore": _iso(filters.updated_before),
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _cursor(record: WorkflowSummary, signature: str) -> str:
    value = json.dumps(
        [signature, record.created_at.isoformat(), record.run_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(value.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _invalid(message: str) -> JsonObject:
    return {"error": {"code": "invalid_input", "message": message}}


def _copy_summary(record: WorkflowSummary) -> WorkflowSummary:
    return replace(
        record,
        node_counts=dict(record.node_counts),
        private_detail=dict(record.private_detail),
    )


def _safe_summary(record: WorkflowSummary) -> WorkflowSummary:
    return replace(
        record,
        node_counts=dict(record.node_counts),
        private_detail={},
    )


def _catalog_key() -> str:
    return "flow-workflow-index"


def _decode_catalog(data: bytes) -> tuple[WorkflowSummary, ...]:
    if not data:
        return ()
    try:
        value = json.loads(data)
        if not isinstance(value, list):
            raise TypeError
        records = tuple(_summary_from_dict(item) for item in value)
        if len({record.run_id for record in records}) != len(records):
            raise ValueError
        return records
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("invalid workflow catalog state") from error


def _summary_from_dict(value: object) -> WorkflowSummary:
    if not isinstance(value, Mapping) or set(value) != {
        "runId",
        "dagDigest",
        "dagName",
        "state",
        "createdAt",
        "updatedAt",
        "completedAt",
        "nodeProgress",
    }:
        raise TypeError
    run_id = value["runId"]
    digest = value["dagDigest"]
    name = value["dagName"]
    state = value["state"]
    counts = value["nodeProgress"]
    if not isinstance(run_id, str) or not isinstance(digest, str):
        raise TypeError
    if name is not None and not isinstance(name, str):
        raise TypeError
    if not isinstance(state, str) or not isinstance(counts, Mapping):
        raise TypeError
    return WorkflowSummary(
        run_id=run_id,
        dag_digest=digest,
        dag_name=name,
        state=cast(RunState, state),
        created_at=datetime.fromisoformat(cast(str, value["createdAt"])),
        updated_at=datetime.fromisoformat(cast(str, value["updatedAt"])),
        completed_at=(
            datetime.fromisoformat(cast(str, value["completedAt"]))
            if value["completedAt"] is not None
            else None
        ),
        node_counts={str(key): item for key, item in counts.items()},
        private_detail={},
    )
