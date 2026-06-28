"""ASK/elicitation durable audit items.

Phase-5 follow-up: raising + resolving an elicitation must persist
``elicitation_request`` / ``elicitation_resolved`` ConversationItems so
the human-in-the-loop proof survives session end (shows in
``GET /v1/sessions/{id}/items``). These exercise the persistence helpers
directly (the full ``_publish_and_wait_for_harness_elicitation``
round-trip needs a live Request + futures + SSE stack).
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from omnigent.entities import ConversationItem, PagedList
from omnigent.entities.conversation import (
    ElicitationRequestData,
    ElicitationResolvedData,
    parse_item_data,
)
from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.schemas import ElicitationRequestParams


class _Store:
    """Append-only in-memory store exposing the two methods used here."""

    def __init__(self) -> None:
        self.items: list[ConversationItem] = []

    def append(self, conversation_id: str, items: list[Any]) -> list[Any]:
        del conversation_id
        out = []
        for item in items:
            persisted = ConversationItem(
                id=f"item_{len(self.items)}",
                type=item.type,
                status="completed",
                response_id=item.response_id,
                created_at=int(time.time()),
                data=item.data,
            )
            self.items.append(persisted)
            out.append(persisted)
        return out


def _params() -> ElicitationRequestParams:
    return ElicitationRequestParams(
        mode="form",
        message="Approve running 'rm -rf /tmp/cache'?",
        requestedSchema={"type": "object"},
        phase="pre_tool_use",
        policy_name="approve_shell_commands",
        content_preview="rm -rf /tmp/cache",
    )


@pytest.mark.asyncio
async def test_raise_and_resolve_persist_two_items() -> None:
    store = _Store()
    eid = "elicit_test_1"

    await sessions_mod._persist_elicitation_request_item(store, "conv_x", eid, _params())
    await sessions_mod._persist_elicitation_resolved_item(store, "conv_x", eid)

    types = [i.type for i in store.items]
    assert types == ["elicitation_request", "elicitation_resolved"]

    req = store.items[0].data
    assert isinstance(req, ElicitationRequestData)
    assert req.elicitation_id == eid
    assert req.message == "Approve running 'rm -rf /tmp/cache'?"
    assert req.policy_name == "approve_shell_commands"

    res = store.items[1].data
    assert isinstance(res, ElicitationResolvedData)
    assert res.elicitation_id == eid

    # Round-trips through the DB-deserialization path the read endpoint uses.
    assert isinstance(
        parse_item_data("elicitation_request", store.items[0].data.model_dump()),
        ElicitationRequestData,
    )


@pytest.mark.asyncio
async def test_repark_republish_does_not_double_write_request() -> None:
    store = _Store()
    eid = "elicit_test_2"

    # Two raises for the same id (a hook-retry re-park republishes the card).
    await sessions_mod._persist_elicitation_request_item(store, "conv_x", eid, _params())
    await sessions_mod._persist_elicitation_request_item(store, "conv_x", eid, _params())
    assert [i.type for i in store.items] == ["elicitation_request"]

    # Resolve clears the dedupe guard so a genuinely new ask can record again.
    await sessions_mod._persist_elicitation_resolved_item(store, "conv_x", eid)
    assert eid not in sessions_mod._persisted_elicitation_request_ids


@pytest.mark.asyncio
async def test_none_store_is_noop() -> None:
    await sessions_mod._persist_elicitation_request_item(None, "conv_x", "e", _params())
    await sessions_mod._persist_elicitation_resolved_item(None, "conv_x", "e")
