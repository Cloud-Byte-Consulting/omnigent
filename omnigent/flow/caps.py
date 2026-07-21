"""Atomic, replay-idempotent cap enforcement for Flow runs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from threading import Lock
from typing import Any, Literal, Protocol, TypeAlias, cast

from dapr.clients.exceptions import DaprInternalError
from dapr.clients.grpc._state import Concurrency, Consistency, StateOptions

from omnigent.flow.audit import AuditEvent, create_audit_event
from omnigent.flow.contracts import RunCaps
from omnigent.flow.usage import UsageService

CapName: TypeAlias = Literal["maxNodes", "maxRounds", "maxConcurrent", "tokenBudget"]
ProposalKind: TypeAlias = Literal["accept_nodes", "dispatch", "complete"]
JsonObject: TypeAlias = dict[str, Any]


@dataclass(frozen=True, slots=True)
class CapDecision:
    """Provider-neutral result of one cap-protected proposal."""

    allowed: bool
    queued: bool
    cap: CapName | None
    current: int
    proposed: int
    limit: int
    message: str

    def to_dict(self) -> JsonObject:
        return {
            "allowed": self.allowed,
            "queued": self.queued,
            "cap": self.cap,
            "current": self.current,
            "proposed": self.proposed,
            "limit": self.limit,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapDecision:
        return cls(
            allowed=cast(bool, value["allowed"]),
            queued=cast(bool, value["queued"]),
            cap=cast(CapName | None, value["cap"]),
            current=cast(int, value["current"]),
            proposed=cast(int, value["proposed"]),
            limit=cast(int, value["limit"]),
            message=cast(str, value["message"]),
        )


@dataclass(frozen=True, slots=True)
class CapProposal:
    """One atomic state transition guarded by run caps."""

    kind: ProposalKind
    idempotency_key: str
    node_ids: tuple[str, ...] = ()
    round_number: int | None = None
    node_id: str | None = None
    required_tokens: int = 0

    @classmethod
    def accept_nodes(
        cls,
        idempotency_key: str,
        node_ids: Sequence[str],
        *,
        round_number: int,
    ) -> CapProposal:
        return cls("accept_nodes", idempotency_key, tuple(node_ids), round_number)

    @classmethod
    def dispatch(
        cls,
        idempotency_key: str,
        node_id: str,
        *,
        required_tokens: int,
    ) -> CapProposal:
        return cls(
            "dispatch",
            idempotency_key,
            node_id=node_id,
            required_tokens=required_tokens,
        )

    @classmethod
    def complete(cls, idempotency_key: str, node_id: str) -> CapProposal:
        return cls("complete", idempotency_key, node_id=node_id)

    def fingerprint(self) -> str:
        canonical = json.dumps(
            {
                "kind": self.kind,
                "nodeIds": list(self.node_ids),
                "roundNumber": self.round_number,
                "nodeId": self.node_id,
                "requiredTokens": self.required_tokens,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CapState:
    """Durable counters, reservations, queue, and replay decisions for one run."""

    run_id: str
    limits: RunCaps
    accepted_node_ids: tuple[str, ...] = ()
    current_round: int = 0
    running_node_ids: tuple[str, ...] = ()
    queued_node_ids: tuple[str, ...] = ()
    reserved_tokens: Mapping[str, int] = field(default_factory=dict)
    decisions: tuple[tuple[str, str, CapDecision], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reserved_tokens", dict(self.reserved_tokens or {}))

    def to_dict(self) -> JsonObject:
        return {
            "runId": self.run_id,
            "limits": self.limits.model_dump(mode="json", by_alias=True),
            "acceptedNodeIds": list(self.accepted_node_ids),
            "currentRound": self.current_round,
            "runningNodeIds": list(self.running_node_ids),
            "queuedNodeIds": list(self.queued_node_ids),
            "reservedTokens": dict(self.reserved_tokens),
            "decisions": [
                {
                    "idempotencyKey": key,
                    "proposalFingerprint": fingerprint,
                    "decision": decision.to_dict(),
                }
                for key, fingerprint, decision in self.decisions
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapState:
        return cls(
            run_id=cast(str, value["runId"]),
            limits=RunCaps.model_validate(value["limits"]),
            accepted_node_ids=tuple(cast(list[str], value["acceptedNodeIds"])),
            current_round=cast(int, value["currentRound"]),
            running_node_ids=tuple(cast(list[str], value["runningNodeIds"])),
            queued_node_ids=tuple(cast(list[str], value["queuedNodeIds"])),
            reserved_tokens={
                str(key): cast(int, item)
                for key, item in cast(Mapping[str, Any], value["reservedTokens"]).items()
            },
            decisions=tuple(
                (
                    cast(str, item["idempotencyKey"]),
                    cast(str, item.get("proposalFingerprint", "")),
                    CapDecision.from_dict(cast(Mapping[str, Any], item["decision"])),
                )
                for item in cast(list[Mapping[str, Any]], value["decisions"])
            ),
        )


class CapStore(Protocol):
    def apply(
        self,
        run_id: str,
        limits: RunCaps,
        proposal: CapProposal,
        *,
        used_tokens: int,
    ) -> CapDecision: ...

    def state(self, run_id: str, limits: RunCaps) -> CapState: ...


class AuditStore(Protocol):
    def append(self, event: AuditEvent) -> AuditEvent: ...


class InMemoryCapStore:
    """Thread-safe atomic store for unit tests and embedded use."""

    def __init__(self) -> None:
        self._states: dict[str, CapState] = {}
        self._lock = Lock()

    def apply(
        self,
        run_id: str,
        limits: RunCaps,
        proposal: CapProposal,
        *,
        used_tokens: int,
    ) -> CapDecision:
        with self._lock:
            state = self._states.get(run_id) or _empty_state(run_id, limits)
            updated, decision, changed = _apply(state, limits, proposal, used_tokens)
            if changed:
                self._states[run_id] = updated
            return decision

    def state(self, run_id: str, limits: RunCaps) -> CapState:
        with self._lock:
            state = self._states.get(run_id) or _empty_state(run_id, limits)
            _require_limits(state, limits)
            return CapState.from_dict(state.to_dict())


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


class DaprCapStore:
    """Dapr state implementation with optimistic atomic ETag updates."""

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

    def apply(
        self,
        run_id: str,
        limits: RunCaps,
        proposal: CapProposal,
        *,
        used_tokens: int,
    ) -> CapDecision:
        key = _state_key(run_id)
        last_error: DaprInternalError | None = None
        for _ in range(self._max_attempts):
            response = self._client.get_state(self._store_name, key)
            state = _decode_state(response.data, run_id, limits)
            updated, decision, changed = _apply(state, limits, proposal, used_tokens)
            if not changed:
                return decision
            try:
                self._client.save_state(
                    self._store_name,
                    key,
                    json.dumps(updated.to_dict(), sort_keys=True, separators=(",", ":")),
                    etag=response.etag or None,
                    options=self._options,
                )
            except DaprInternalError as error:
                last_error = error
                continue
            return decision
        if last_error is not None:
            raise last_error
        raise RuntimeError("cap update failed without a Dapr error")

    def state(self, run_id: str, limits: RunCaps) -> CapState:
        response = self._client.get_state(self._store_name, _state_key(run_id))
        return _decode_state(response.data, run_id, limits)


class CapService:
    """Single boundary for cap-protected acceptance and dispatch."""

    def __init__(
        self,
        store: CapStore,
        *,
        usage: UsageService,
        audit: AuditStore,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._usage = usage
        self._audit = audit
        self._clock = clock

    def accept_nodes(
        self,
        run_id: str,
        limits: RunCaps,
        *,
        node_ids: Sequence[str],
        round_number: int,
        idempotency_key: str,
    ) -> CapDecision:
        decision = self._store.apply(
            run_id,
            limits,
            CapProposal.accept_nodes(idempotency_key, node_ids, round_number=round_number),
            used_tokens=self._used_tokens(run_id, limits),
        )
        self._audit_denial(run_id, idempotency_key, decision)
        return decision

    def request_dispatch(
        self,
        run_id: str,
        limits: RunCaps,
        *,
        node_id: str,
        required_tokens: int,
        idempotency_key: str,
    ) -> CapDecision:
        decision = self._store.apply(
            run_id,
            limits,
            CapProposal.dispatch(
                idempotency_key,
                node_id,
                required_tokens=required_tokens,
            ),
            used_tokens=self._used_tokens(run_id, limits),
        )
        self._audit_denial(run_id, idempotency_key, decision)
        return decision

    def complete_dispatch(
        self,
        run_id: str,
        limits: RunCaps,
        *,
        node_id: str,
        idempotency_key: str,
    ) -> CapDecision:
        return self._store.apply(
            run_id,
            limits,
            CapProposal.complete(idempotency_key, node_id),
            used_tokens=self._used_tokens(run_id, limits),
        )

    def _used_tokens(self, run_id: str, limits: RunCaps) -> int:
        return self._usage.state(run_id, token_budget=limits.token_budget).used_tokens

    def _audit_denial(
        self,
        run_id: str,
        idempotency_key: str,
        decision: CapDecision,
    ) -> None:
        if decision.allowed or decision.queued or decision.cap is None:
            return
        self._audit.append(
            create_audit_event(
                run_id=run_id,
                node_id=None,
                event_type="cap_denial",
                timestamp=self._clock(),
                source="cap_policy",
                correlation_key=idempotency_key,
                summary=decision.message,
                metadata={
                    "cap": decision.cap,
                    "current": decision.current,
                    "proposed": decision.proposed,
                    "limit": decision.limit,
                },
            )
        )


def _apply(
    state: CapState,
    limits: RunCaps,
    proposal: CapProposal,
    used_tokens: int,
) -> tuple[CapState, CapDecision, bool]:
    _require_limits(state, limits)
    _require_proposal(proposal, used_tokens)
    replay = next(
        (item for item in state.decisions if item[0] == proposal.idempotency_key),
        None,
    )
    if replay is not None:
        _, fingerprint, decision = replay
        if fingerprint and fingerprint != proposal.fingerprint():
            raise ValueError("idempotency key was reused for a different cap proposal")
        return state, decision, False
    if proposal.kind == "accept_nodes":
        return _accept_nodes(state, proposal)
    if proposal.kind == "dispatch":
        return _dispatch(state, proposal, used_tokens)
    return _complete(state, proposal)


def _accept_nodes(
    state: CapState,
    proposal: CapProposal,
) -> tuple[CapState, CapDecision, bool]:
    assert proposal.round_number is not None
    new_ids = tuple(item for item in proposal.node_ids if item not in state.accepted_node_ids)
    if not new_ids:
        raise ValueError("node acceptance must add at least one new node")
    current_nodes = len(state.accepted_node_ids)
    proposed_nodes = current_nodes + len(new_ids)
    if proposed_nodes > state.limits.max_nodes:
        return _remember(
            state,
            proposal,
            _denied("maxNodes", current_nodes, proposed_nodes, state.limits.max_nodes),
        )
    if proposal.round_number > state.limits.max_rounds:
        return _remember(
            state,
            proposal,
            _denied(
                "maxRounds",
                state.current_round,
                proposal.round_number,
                state.limits.max_rounds,
            ),
        )
    if proposal.round_number != state.current_round + 1:
        raise ValueError("round_number must increment exactly once")
    decision = CapDecision(
        True,
        False,
        None,
        current_nodes,
        proposed_nodes,
        state.limits.max_nodes,
        "nodes accepted within run caps",
    )
    updated = replace(
        state,
        accepted_node_ids=(*state.accepted_node_ids, *new_ids),
        current_round=proposal.round_number,
        queued_node_ids=(*state.queued_node_ids, *new_ids),
    )
    return _remember(updated, proposal, decision)


def _dispatch(
    state: CapState,
    proposal: CapProposal,
    used_tokens: int,
) -> tuple[CapState, CapDecision, bool]:
    assert proposal.node_id is not None
    if proposal.node_id not in state.accepted_node_ids:
        raise ValueError("dispatch node must be accepted before cap evaluation")
    reserved = sum(state.reserved_tokens.values())
    current_tokens = used_tokens + reserved
    if proposal.node_id in state.running_node_ids:
        decision = CapDecision(
            True,
            False,
            "tokenBudget",
            current_tokens,
            current_tokens,
            state.limits.token_budget,
            "node dispatch is already reserved",
        )
        return _remember(state, proposal, decision)
    proposed_tokens = current_tokens + proposal.required_tokens
    if proposed_tokens > state.limits.token_budget:
        return _remember(
            state,
            proposal,
            _denied("tokenBudget", current_tokens, proposed_tokens, state.limits.token_budget),
        )
    current_running = len(state.running_node_ids)
    if current_running >= state.limits.max_concurrent:
        queued = (
            state.queued_node_ids
            if proposal.node_id in state.queued_node_ids
            else (*state.queued_node_ids, proposal.node_id)
        )
        decision = CapDecision(
            False,
            True,
            "maxConcurrent",
            current_running,
            current_running + 1,
            state.limits.max_concurrent,
            "node is queued until concurrency capacity is available",
        )
        return replace(state, queued_node_ids=queued), decision, queued != state.queued_node_ids

    reservations = dict(state.reserved_tokens)
    reservations[proposal.node_id] = proposal.required_tokens
    decision = CapDecision(
        True,
        False,
        "tokenBudget",
        current_tokens,
        proposed_tokens,
        state.limits.token_budget,
        "node dispatch accepted within run caps",
    )
    updated = replace(
        state,
        running_node_ids=(*state.running_node_ids, proposal.node_id),
        queued_node_ids=tuple(item for item in state.queued_node_ids if item != proposal.node_id),
        reserved_tokens=reservations,
    )
    return _remember(updated, proposal, decision)


def _complete(
    state: CapState,
    proposal: CapProposal,
) -> tuple[CapState, CapDecision, bool]:
    assert proposal.node_id is not None
    reservations = dict(state.reserved_tokens)
    reservations.pop(proposal.node_id, None)
    updated = replace(
        state,
        running_node_ids=tuple(
            item for item in state.running_node_ids if item != proposal.node_id
        ),
        queued_node_ids=tuple(item for item in state.queued_node_ids if item != proposal.node_id),
        reserved_tokens=reservations,
    )
    decision = CapDecision(
        True,
        False,
        None,
        len(state.running_node_ids),
        len(updated.running_node_ids),
        state.limits.max_concurrent,
        "node dispatch reservation released",
    )
    return _remember(updated, proposal, decision)


def _remember(
    state: CapState,
    proposal: CapProposal,
    decision: CapDecision,
) -> tuple[CapState, CapDecision, bool]:
    return (
        replace(
            state,
            decisions=(
                *state.decisions,
                (proposal.idempotency_key, proposal.fingerprint(), decision),
            ),
        ),
        decision,
        True,
    )


def _denied(cap: CapName, current: int, proposed: int, limit: int) -> CapDecision:
    return CapDecision(
        False,
        False,
        cap,
        current,
        proposed,
        limit,
        f"proposal exceeds {cap}",
    )


def _require_proposal(proposal: CapProposal, used_tokens: int) -> None:
    if not proposal.idempotency_key:
        raise ValueError("idempotency_key is required")
    if used_tokens < 0:
        raise ValueError("used_tokens cannot be negative")
    if proposal.kind == "accept_nodes":
        if not proposal.node_ids or len(set(proposal.node_ids)) != len(proposal.node_ids):
            raise ValueError("node_ids must be non-empty and unique")
        if any(not item for item in proposal.node_ids) or not proposal.round_number:
            raise ValueError("node IDs and a positive round_number are required")
    elif not proposal.node_id:
        raise ValueError("node_id is required")
    if proposal.kind == "dispatch" and proposal.required_tokens <= 0:
        raise ValueError("required_tokens must be positive")


def _empty_state(run_id: str, limits: RunCaps) -> CapState:
    if not run_id:
        raise ValueError("run_id is required")
    return CapState(run_id=run_id, limits=limits)


def _require_limits(state: CapState, limits: RunCaps) -> None:
    if state.limits != limits:
        raise ValueError("run caps cannot change for an existing run")


def _state_key(run_id: str) -> str:
    return f"flow-caps:{run_id}"


def _decode_state(value: bytes, run_id: str, limits: RunCaps) -> CapState:
    if not value:
        return _empty_state(run_id, limits)
    decoded = json.loads(value)
    if not isinstance(decoded, dict) or decoded.get("runId") != run_id:
        raise ValueError("persisted cap state does not match the requested run")
    state = CapState.from_dict(decoded)
    _require_limits(state, limits)
    return state
