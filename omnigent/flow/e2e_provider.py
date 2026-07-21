"""Durable deterministic provider used by the local Flow end-to-end harness."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from dapr.clients.exceptions import DaprInternalError
from dapr.clients.grpc._state import Concurrency, Consistency, StateOptions

from omnigent.flow.providers import AdapterRequest, AdapterResponse, TokenUsage

JsonObject = dict[str, Any]


@dataclass(frozen=True, slots=True)
class EffectRecord:
    """Durable evidence for delivery and logical side-effect idempotency."""

    node_execution_id: str
    run_id: str
    node_id: str
    delivery_count: int
    effect_count: int
    completed: bool
    output: Any

    def to_dict(self) -> JsonObject:
        return {
            "nodeExecutionId": self.node_execution_id,
            "runId": self.run_id,
            "nodeId": self.node_id,
            "deliveryCount": self.delivery_count,
            "effectCount": self.effect_count,
            "completed": self.completed,
            "output": self.output,
        }


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


class DaprDeterministicAdapter:
    """A fake provider whose durable marker makes redelivery observable and safe."""

    def __init__(
        self,
        client: DaprStateClient,
        *,
        store_name: str = "flowstatestore",
        slow_node: str | None = None,
        delay_seconds: float = 0,
        max_attempts: int = 5,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self._client = client
        self._store_name = store_name
        self._slow_node = slow_node
        self._delay_seconds = delay_seconds
        self._max_attempts = max_attempts
        self._options = StateOptions(
            consistency=Consistency.strong,
            concurrency=Concurrency.first_write,
        )

    async def execute(self, request: AdapterRequest, *, credential: str) -> AdapterResponse:
        """Record a delivery, pause the configured first delivery, and complete once."""
        if not credential:
            raise ValueError("credential is required")
        if not request.node_execution_id:
            raise ValueError("node_execution_id is required")

        record, first_delivery = self._begin_delivery(request)
        if record.completed:
            return _response(record.output)
        if (
            first_delivery
            and request.node_id == self._slow_node
            and self._delay_seconds > 0
        ):
            await asyncio.sleep(self._delay_seconds)
        completed = self._complete(request.node_execution_id)
        return _response(completed.output)

    def effect(self, node_execution_id: str) -> EffectRecord:
        """Read persisted harness evidence for one stable node execution."""
        response = self._client.get_state(
            self._store_name,
            _state_key(node_execution_id),
        )
        record = _decode_record(response.data)
        if record is None:
            raise KeyError(node_execution_id)
        if record.node_execution_id != node_execution_id:
            raise RuntimeError("invalid deterministic effect state: identity mismatch")
        return record

    def _begin_delivery(self, request: AdapterRequest) -> tuple[EffectRecord, bool]:
        assert request.node_execution_id is not None
        key = _state_key(request.node_execution_id)
        last_error: DaprInternalError | None = None
        for _ in range(self._max_attempts):
            response = self._client.get_state(self._store_name, key)
            current = _decode_record(response.data)
            if current is None:
                updated = EffectRecord(
                    node_execution_id=request.node_execution_id,
                    run_id=request.run_id,
                    node_id=request.node_id,
                    delivery_count=1,
                    effect_count=1,
                    completed=False,
                    output=_output(request),
                )
                first_delivery = True
            else:
                _require_identity(current, request)
                updated = EffectRecord(
                    node_execution_id=current.node_execution_id,
                    run_id=current.run_id,
                    node_id=current.node_id,
                    delivery_count=current.delivery_count + 1,
                    effect_count=current.effect_count,
                    completed=current.completed,
                    output=current.output,
                )
                first_delivery = False
            try:
                self._save(key, updated, etag=response.etag or None)
            except DaprInternalError as error:
                last_error = error
                continue
            return updated, first_delivery
        if last_error is not None:
            raise last_error
        raise RuntimeError("deterministic delivery update failed without a Dapr error")

    def _complete(self, node_execution_id: str) -> EffectRecord:
        key = _state_key(node_execution_id)
        last_error: DaprInternalError | None = None
        for _ in range(self._max_attempts):
            response = self._client.get_state(self._store_name, key)
            current = _decode_record(response.data)
            if current is None or current.node_execution_id != node_execution_id:
                raise RuntimeError("invalid deterministic effect state: completion marker missing")
            if current.completed:
                return current
            updated = EffectRecord(
                node_execution_id=current.node_execution_id,
                run_id=current.run_id,
                node_id=current.node_id,
                delivery_count=current.delivery_count,
                effect_count=current.effect_count,
                completed=True,
                output=current.output,
            )
            try:
                self._save(key, updated, etag=response.etag or None)
            except DaprInternalError as error:
                last_error = error
                continue
            return updated
        if last_error is not None:
            raise last_error
        raise RuntimeError("deterministic completion failed without a Dapr error")

    def _save(self, key: str, record: EffectRecord, *, etag: str | None) -> None:
        self._client.save_state(
            self._store_name,
            key,
            json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")),
            etag=etag,
            options=self._options,
        )


def _output(request: AdapterRequest) -> JsonObject:
    if request.dependency_outputs:
        values: list[str] = []
        for dependency in sorted(request.dependency_outputs):
            result = request.dependency_outputs[dependency]
            if not isinstance(result, Mapping) or not isinstance(result.get("value"), str):
                raise RuntimeError("deterministic dependency output must contain a string value")
            values.append(cast(str, result["value"]))
        return {"values": values}
    return {"value": request.node_id}


def _response(output: Any) -> AdapterResponse:
    return AdapterResponse(
        output=output,
        usage=TokenUsage(input_tokens=1, output_tokens=0, total_tokens=1),
        latency_ms=0,
    )


def _state_key(node_execution_id: str) -> str:
    return f"flow-fake-effect:{node_execution_id}"


def _require_identity(record: EffectRecord, request: AdapterRequest) -> None:
    if (
        record.node_execution_id != request.node_execution_id
        or record.run_id != request.run_id
        or record.node_id != request.node_id
    ):
        raise RuntimeError("invalid deterministic effect state: identity mismatch")


def _decode_record(data: bytes) -> EffectRecord | None:
    if not data:
        return None
    try:
        value = json.loads(data)
        if not isinstance(value, Mapping):
            raise TypeError
        expected = {
            "nodeExecutionId",
            "runId",
            "nodeId",
            "deliveryCount",
            "effectCount",
            "completed",
            "output",
        }
        if set(value) != expected:
            raise TypeError
        node_execution_id = value["nodeExecutionId"]
        run_id = value["runId"]
        node_id = value["nodeId"]
        delivery_count = value["deliveryCount"]
        effect_count = value["effectCount"]
        completed = value["completed"]
        if not all(
            isinstance(item, str) and item
            for item in (node_execution_id, run_id, node_id)
        ):
            raise TypeError
        if not isinstance(delivery_count, int) or isinstance(delivery_count, bool):
            raise TypeError
        if not isinstance(effect_count, int) or isinstance(effect_count, bool):
            raise TypeError
        if delivery_count <= 0 or effect_count != 1 or not isinstance(completed, bool):
            raise TypeError
        return EffectRecord(
            node_execution_id=node_execution_id,
            run_id=run_id,
            node_id=node_id,
            delivery_count=delivery_count,
            effect_count=effect_count,
            completed=completed,
            output=value["output"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("invalid deterministic effect state") from error
