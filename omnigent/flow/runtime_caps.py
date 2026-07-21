"""Replay-safe Dapr activity for durable Flow cap transitions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.flow.caps import AuditStore, CapService, CapStore
from omnigent.flow.contracts import RunCaps
from omnigent.flow.usage import UsageService

RUNTIME_CAP_ACTIVITY_NAME = "ApplyFlowCapTransition"
JsonObject = dict[str, Any]


def _camel_case(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class RuntimeCapInput(BaseModel):
    """Strict JSON-safe request for one atomic cap transition."""

    model_config = ConfigDict(
        alias_generator=_camel_case,
        extra="forbid",
        populate_by_name=True,
        strict=True,
    )

    run_id: str = Field(min_length=1)
    limits: RunCaps
    kind: Literal["accept_nodes", "dispatch", "complete"]
    idempotency_key: str = Field(min_length=1)
    node_ids: list[str] = Field(default_factory=list)
    round_number: int | None = Field(default=None, gt=0)
    node_id: str | None = None
    required_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_transition(self) -> RuntimeCapInput:
        if self.kind == "accept_nodes":
            if (
                not self.node_ids
                or len(self.node_ids) != len(set(self.node_ids))
                or any(not node_id for node_id in self.node_ids)
                or self.round_number is None
                or self.node_id is not None
                or self.required_tokens != 0
            ):
                raise ValueError("accept_nodes requires unique nodes and one positive round")
        elif (
            not self.node_id
            or self.node_ids
            or self.round_number is not None
            or (self.kind == "dispatch" and self.required_tokens <= 0)
            or (self.kind == "complete" and self.required_tokens != 0)
        ):
            raise ValueError("dispatch or complete requires exactly one node")
        return self


class ActivityRuntime(Protocol):
    def register_activity(
        self,
        fn: Callable[..., JsonObject],
        *,
        name: str | None = None,
    ) -> object: ...


class RuntimeCapActivity:
    """Apply one idempotent cap proposal and return its durable snapshot."""

    def __init__(
        self,
        store: CapStore,
        *,
        usage: UsageService,
        audit: AuditStore,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._service = CapService(store, usage=usage, audit=audit, clock=clock)

    def execute(self, raw_input: RuntimeCapInput | Mapping[str, Any]) -> JsonObject:
        value = (
            raw_input
            if isinstance(raw_input, RuntimeCapInput)
            else RuntimeCapInput.model_validate(raw_input)
        )
        if value.kind == "accept_nodes":
            assert value.round_number is not None
            decision = self._service.accept_nodes(
                value.run_id,
                value.limits,
                node_ids=value.node_ids,
                round_number=value.round_number,
                idempotency_key=value.idempotency_key,
            )
        elif value.kind == "dispatch":
            assert value.node_id is not None
            decision = self._service.request_dispatch(
                value.run_id,
                value.limits,
                node_id=value.node_id,
                required_tokens=value.required_tokens,
                idempotency_key=value.idempotency_key,
            )
        else:
            assert value.node_id is not None
            decision = self._service.complete_dispatch(
                value.run_id,
                value.limits,
                node_id=value.node_id,
                idempotency_key=value.idempotency_key,
            )
        return {
            "decision": decision.to_dict(),
            "state": self._store.state(value.run_id, value.limits).to_dict(),
        }


def register_runtime_cap_activity(
    runtime: ActivityRuntime,
    activity: RuntimeCapActivity,
) -> Callable[[object, RuntimeCapInput], JsonObject]:
    """Register the synchronous cap transition adapter used by Dapr Workflow."""

    def apply_flow_cap_transition(
        _context: object,
        activity_input: RuntimeCapInput,
    ) -> JsonObject:
        return activity.execute(activity_input)

    runtime.register_activity(apply_flow_cap_transition, name=RUNTIME_CAP_ACTIVITY_NAME)
    return apply_flow_cap_transition
