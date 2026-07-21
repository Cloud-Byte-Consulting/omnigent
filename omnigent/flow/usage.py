"""Durable, replay-idempotent token accounting for Flow runs."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol, TypeAlias, cast

from dapr.clients.exceptions import DaprInternalError
from dapr.clients.grpc._state import Concurrency, Consistency, StateOptions

from omnigent.flow.providers import TokenUsage

JsonObject: TypeAlias = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ConservativeUsagePolicy:
    """Fallback charge applied when a provider reports no usable total."""

    tokens_per_attempt: int

    def __post_init__(self) -> None:
        if self.tokens_per_attempt <= 0:
            raise ValueError("tokens_per_attempt must be positive")


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """Normalized usage for one provider attempt."""

    record_id: str
    idempotency_key: str
    run_id: str
    node_id: str
    attempt: int
    provider: str
    model: str
    succeeded: bool
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int
    extra_tokens: dict[str, int]
    estimated: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> JsonObject:
        return {
            "recordId": self.record_id,
            "idempotencyKey": self.idempotency_key,
            "runId": self.run_id,
            "nodeId": self.node_id,
            "attempt": self.attempt,
            "provider": self.provider,
            "model": self.model,
            "succeeded": self.succeeded,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "totalTokens": self.total_tokens,
            "extraTokens": dict(self.extra_tokens),
            "estimated": self.estimated,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> UsageRecord:
        return cls(
            record_id=cast(str, value["recordId"]),
            idempotency_key=cast(str, value["idempotencyKey"]),
            run_id=cast(str, value["runId"]),
            node_id=cast(str, value["nodeId"]),
            attempt=cast(int, value["attempt"]),
            provider=cast(str, value["provider"]),
            model=cast(str, value["model"]),
            succeeded=cast(bool, value["succeeded"]),
            input_tokens=cast(int | None, value["inputTokens"]),
            output_tokens=cast(int | None, value["outputTokens"]),
            total_tokens=cast(int, value["totalTokens"]),
            extra_tokens={
                str(key): cast(int, item)
                for key, item in cast(Mapping[str, Any], value["extraTokens"]).items()
            },
            estimated=cast(bool, value["estimated"]),
            warnings=tuple(cast(list[str], value["warnings"])),
        )


@dataclass(frozen=True, slots=True)
class RunUsageState:
    """Current durable budget view for one run."""

    run_id: str
    limit_tokens: int
    used_tokens: int
    remaining_tokens: int
    cap_reached: bool
    records: tuple[UsageRecord, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BudgetFailure:
    code: str
    category: str
    retryable: bool
    message: str
    current: int
    remaining: int
    limit: int


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    allowed: bool
    failure: BudgetFailure | None


class UsageStore(Protocol):
    def append(self, record: UsageRecord, *, token_budget: int) -> RunUsageState: ...

    def state(self, run_id: str, *, token_budget: int) -> RunUsageState: ...


class InMemoryUsageStore:
    """Thread-safe store for unit tests and embedded use."""

    def __init__(self) -> None:
        self._states: dict[str, RunUsageState] = {}
        self._lock = Lock()

    def append(self, record: UsageRecord, *, token_budget: int) -> RunUsageState:
        with self._lock:
            state = self._states.get(record.run_id) or _empty_state(record.run_id, token_budget)
            _require_same_budget(state, token_budget)
            if any(item.record_id == record.record_id for item in state.records):
                return _copy_state(state)
            updated = _state_from_records(
                record.run_id,
                token_budget,
                (*state.records, _copy_record(record)),
            )
            self._states[record.run_id] = updated
            return _copy_state(updated)

    def state(self, run_id: str, *, token_budget: int) -> RunUsageState:
        with self._lock:
            state = self._states.get(run_id) or _empty_state(run_id, token_budget)
            _require_same_budget(state, token_budget)
            return _copy_state(state)


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


class DaprUsageStore:
    """Durable Dapr state implementation with optimistic ETag retries."""

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
        self._lock = Lock()
        self._options = StateOptions(
            consistency=Consistency.strong,
            concurrency=Concurrency.first_write,
        )

    def append(self, record: UsageRecord, *, token_budget: int) -> RunUsageState:
        with self._lock:
            return self._append(record, token_budget=token_budget)

    def _append(self, record: UsageRecord, *, token_budget: int) -> RunUsageState:
        key = _state_key(record.run_id)
        last_error: DaprInternalError | None = None
        for attempt in range(self._max_attempts):
            response = self._client.get_state(self._store_name, key)
            state = _decode_state(response.data, record.run_id, token_budget)
            if any(item.record_id == record.record_id for item in state.records):
                return _copy_state(state)
            updated = _state_from_records(
                record.run_id,
                token_budget,
                (*state.records, _copy_record(record)),
            )
            try:
                self._client.save_state(
                    self._store_name,
                    key,
                    json.dumps(_state_to_dict(updated), sort_keys=True, separators=(",", ":")),
                    etag=response.etag or None,
                    options=self._options,
                )
            except DaprInternalError as error:
                last_error = error
                time.sleep(min(0.01 * (attempt + 1), 0.05))
                continue
            return _copy_state(updated)
        if last_error is not None:
            raise last_error
        raise RuntimeError("usage append failed without a Dapr error")

    def state(self, run_id: str, *, token_budget: int) -> RunUsageState:
        response = self._client.get_state(self._store_name, _state_key(run_id))
        return _copy_state(_decode_state(response.data, run_id, token_budget))


class UsageService:
    """Normalize attempt usage and make pre-dispatch budget decisions."""

    def __init__(
        self,
        store: UsageStore,
        *,
        missing_usage_policy: ConservativeUsagePolicy,
    ) -> None:
        self._store = store
        self._missing_usage_policy = missing_usage_policy

    def record_attempt(
        self,
        *,
        run_id: str,
        idempotency_key: str,
        node_id: str,
        attempt: int,
        provider: str,
        model: str,
        succeeded: bool,
        usage: TokenUsage | None,
        token_budget: int,
        extra_tokens: Mapping[str, int] | None = None,
    ) -> RunUsageState:
        """Persist one normalized attempt before another budget-dependent dispatch."""
        _require_positive_budget(token_budget)
        if not all((run_id, idempotency_key, node_id, provider, model)) or attempt <= 0:
            raise ValueError("usage identity fields and a positive attempt are required")
        extras = dict(extra_tokens or {})
        if any(value < 0 for value in extras.values()):
            raise ValueError("extra token counts cannot be negative")
        input_tokens, output_tokens, total_tokens, estimated, warnings = self._normalize(usage)
        record = UsageRecord(
            record_id=_record_id(run_id, idempotency_key),
            idempotency_key=idempotency_key,
            run_id=run_id,
            node_id=node_id,
            attempt=attempt,
            provider=provider,
            model=model,
            succeeded=succeeded,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            extra_tokens=extras,
            estimated=estimated,
            warnings=warnings,
        )
        return self._store.append(record, token_budget=token_budget)

    def check_dispatch(
        self,
        run_id: str,
        *,
        token_budget: int,
        required_tokens: int,
    ) -> BudgetDecision:
        """Refuse dispatch when policy says the next call cannot fit."""
        _require_positive_budget(token_budget)
        if required_tokens <= 0:
            raise ValueError("required_tokens must be positive")
        state = self._store.state(run_id, token_budget=token_budget)
        if not state.cap_reached and required_tokens <= state.remaining_tokens:
            return BudgetDecision(allowed=True, failure=None)
        return BudgetDecision(
            allowed=False,
            failure=BudgetFailure(
                code="budget_exceeded",
                category="budget",
                retryable=False,
                message="requested provider call exceeds the remaining token budget",
                current=state.used_tokens,
                remaining=state.remaining_tokens,
                limit=state.limit_tokens,
            ),
        )

    def state(self, run_id: str, *, token_budget: int) -> RunUsageState:
        """Return the durable normalized usage snapshot used by cap policy."""
        _require_positive_budget(token_budget)
        return self._store.state(run_id, token_budget=token_budget)

    def _normalize(
        self,
        usage: TokenUsage | None,
    ) -> tuple[int | None, int | None, int, bool, tuple[str, ...]]:
        if usage is not None:
            values = (usage.input_tokens, usage.output_tokens, usage.total_tokens)
            if any(value is not None and value < 0 for value in values):
                raise ValueError("provider token counts cannot be negative")
            if usage.total_tokens is not None:
                return usage.input_tokens, usage.output_tokens, usage.total_tokens, False, ()
            reported = [
                value for value in (usage.input_tokens, usage.output_tokens) if value is not None
            ]
            if reported:
                return usage.input_tokens, usage.output_tokens, sum(reported), False, ()
        estimate = self._missing_usage_policy.tokens_per_attempt
        return (
            None,
            None,
            estimate,
            True,
            (f"provider usage unavailable; counted {estimate} tokens",),
        )


def _record_id(run_id: str, idempotency_key: str) -> str:
    return hashlib.sha256(f"{run_id}\0{idempotency_key}".encode()).hexdigest()


def _state_from_records(
    run_id: str,
    token_budget: int,
    records: tuple[UsageRecord, ...],
) -> RunUsageState:
    used = sum(record.total_tokens for record in records)
    warnings = tuple(dict.fromkeys(warning for record in records for warning in record.warnings))
    return RunUsageState(
        run_id=run_id,
        limit_tokens=token_budget,
        used_tokens=used,
        remaining_tokens=max(token_budget - used, 0),
        cap_reached=used >= token_budget,
        records=records,
        warnings=warnings,
    )


def _empty_state(run_id: str, token_budget: int) -> RunUsageState:
    _require_positive_budget(token_budget)
    return _state_from_records(run_id, token_budget, ())


def _require_positive_budget(token_budget: int) -> None:
    if token_budget <= 0:
        raise ValueError("token_budget must be positive")


def _require_same_budget(state: RunUsageState, token_budget: int) -> None:
    if state.limit_tokens != token_budget:
        raise ValueError("token budget cannot change for an existing run")


def _state_key(run_id: str) -> str:
    return f"flow-usage:{run_id}"


def _state_to_dict(state: RunUsageState) -> JsonObject:
    return {
        "runId": state.run_id,
        "limitTokens": state.limit_tokens,
        "records": [record.to_dict() for record in state.records],
    }


def _decode_state(value: bytes, run_id: str, token_budget: int) -> RunUsageState:
    if not value:
        return _empty_state(run_id, token_budget)
    decoded = json.loads(value)
    if not isinstance(decoded, dict) or decoded.get("runId") != run_id:
        raise ValueError("persisted usage state does not match the requested run")
    if decoded.get("limitTokens") != token_budget:
        raise ValueError("token budget cannot change for an existing run")
    records = tuple(UsageRecord.from_dict(item) for item in decoded.get("records", []))
    return _state_from_records(run_id, token_budget, records)


def _copy_record(record: UsageRecord) -> UsageRecord:
    return UsageRecord.from_dict(record.to_dict())


def _copy_state(state: RunUsageState) -> RunUsageState:
    return _state_from_records(
        state.run_id,
        state.limit_tokens,
        tuple(_copy_record(record) for record in state.records),
    )
