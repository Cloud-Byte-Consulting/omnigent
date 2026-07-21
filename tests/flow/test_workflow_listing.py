import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
from dapr.clients.exceptions import DaprInternalError
from dapr.clients.grpc._state import StateOptions

from omnigent.flow.listing import (
    DaprWorkflowCatalog,
    InMemoryWorkflowCatalog,
    WorkflowListingService,
    WorkflowSummary,
)
from omnigent.flow.mcp_listing import ListingFlowService
from omnigent.flow.mcp_server import create_server
from omnigent.flow.mcp_status import StatusFlowService

NOW = datetime(2026, 7, 21, tzinfo=UTC)


@dataclass
class StateResponse:
    data: bytes
    etag: str


class FakeDaprStateClient:
    def __init__(self) -> None:
        self.value = b""
        self.version = 0
        self.saves: list[tuple[str, str, bytes | str, StateOptions]] = []

    def get_state(self, store_name: str, key: str) -> StateResponse:
        del store_name, key
        return StateResponse(self.value, str(self.version) if self.version else "")

    def save_state(self, store_name, key, value, *, etag, options):
        expected = str(self.version) if self.version else None
        if etag != expected:
            raise DaprInternalError("etag mismatch")
        self.value = value.encode() if isinstance(value, str) else value
        self.version += 1
        self.saves.append((store_name, key, value, options))


def summary(
    run_id: str,
    *,
    minutes: int,
    state: str = "running",
    sensitive: bool = False,
) -> WorkflowSummary:
    raw_detail = (
        {
            "instructions": "secret prompt",
            "output": {"token": "sk-secret-value"},
            "approvalToken": "signed-secret",
            "credential": "password",
            "rawFailure": "provider payload",
        }
        if sensitive
        else {}
    )
    return WorkflowSummary(
        run_id=run_id,
        dag_digest=f"digest-{run_id}",
        dag_name=f"DAG {run_id}",
        state=state,
        created_at=NOW + timedelta(minutes=minutes),
        updated_at=NOW + timedelta(minutes=minutes, seconds=30),
        completed_at=(NOW + timedelta(minutes=minutes, seconds=30))
        if state in {"succeeded", "failed", "canceled", "rejected"}
        else None,
        node_counts={
            "total": 3,
            "blocked": 0,
            "queued": 1 if state == "running" else 0,
            "running": 1 if state == "running" else 0,
            "succeeded": 3 if state == "succeeded" else 1,
            "failed": 1 if state == "failed" else 0,
            "canceled": 0,
            "skipped": 1 if state == "failed" else 0,
        },
        private_detail=raw_detail,
    )


def service() -> WorkflowListingService:
    catalog = InMemoryWorkflowCatalog(
        [
            summary("run-c", minutes=2, state="succeeded", sensitive=True),
            summary("hidden", minutes=1),
            summary("run-b", minutes=1, state="failed"),
            summary("run-a", minutes=1),
            summary("run-d", minutes=3),
        ]
    )
    return WorkflowListingService(
        catalog,
        authorizer=lambda actor, run_id: actor == "operator" and run_id != "hidden",
    )


def test_authorized_pagination_is_stable_complete_and_has_no_duplicates() -> None:
    listing = service()

    first = listing.list(actor="operator", limit=2)
    second = listing.list(actor="operator", limit=2, cursor=first["nextCursor"])

    assert [item["runId"] for item in first["workflows"]] == ["run-a", "run-b"]
    assert [item["runId"] for item in second["workflows"]] == ["run-c", "run-d"]
    assert first["visibleCount"] == second["visibleCount"] == 4
    assert first["nextCursor"]
    assert second["nextCursor"] is None
    assert not ({item["runId"] for item in first["workflows"]} & {
        item["runId"] for item in second["workflows"]
    })


def test_state_and_timestamp_filters_apply_before_page_counts() -> None:
    result = service().list(
        actor="operator",
        state="succeeded",
        created_after=(NOW + timedelta(minutes=1, seconds=30)).isoformat(),
        created_before=(NOW + timedelta(minutes=2, seconds=30)).isoformat(),
        updated_after=(NOW + timedelta(minutes=2)).isoformat(),
        updated_before=(NOW + timedelta(minutes=3)).isoformat(),
        limit=10,
    )

    assert [item["runId"] for item in result["workflows"]] == ["run-c"]
    assert result["visibleCount"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "not-a-state"},
        {"cursor": "not-a-cursor"},
        {"created_after": "yesterday"},
        {
            "created_after": (NOW + timedelta(days=1)).isoformat(),
            "created_before": NOW.isoformat(),
        },
        {"limit": 0},
        {"limit": 101},
    ],
)
def test_invalid_filters_return_one_canonical_error(changes) -> None:
    result = service().list(actor="operator", **changes)

    assert result["error"]["code"] == "invalid_input"
    assert set(result) == {"error"}


def test_summary_excludes_every_sensitive_detail_field() -> None:
    result = service().list(actor="operator", limit=10)
    serialized = json.dumps(result)

    assert "instructions" not in serialized
    assert "output" not in serialized
    assert "approvalToken" not in serialized
    assert "credential" not in serialized
    assert "rawFailure" not in serialized
    assert "sk-secret-value" not in serialized
    assert set(result["workflows"][0]) == {
        "runId",
        "dagDigest",
        "dagName",
        "state",
        "createdAt",
        "updatedAt",
        "completedAt",
        "nodeProgress",
    }


def test_dapr_catalog_persists_latest_safe_summary_and_ignores_stale_replay() -> None:
    client = FakeDaprStateClient()
    catalog = DaprWorkflowCatalog(client)
    original = summary("run-a", minutes=1, sensitive=True)

    catalog.upsert(original)
    catalog.upsert(original)
    catalog.upsert(replace(original, updated_at=original.updated_at - timedelta(seconds=1)))
    latest = replace(original, updated_at=original.updated_at + timedelta(seconds=1))
    catalog.upsert(latest)

    restored = DaprWorkflowCatalog(client).snapshot()
    assert len(restored) == 1
    assert restored[0].updated_at == latest.updated_at
    assert restored[0].private_detail == {}
    assert len(client.saves) == 2
    assert client.saves[0][0:2] == ("flowstatestore", "flow-workflow-index")
    assert "sk-secret-value" not in client.value.decode()


def test_dapr_catalog_rejects_conflicting_same_timestamp_replay() -> None:
    client = FakeDaprStateClient()
    catalog = DaprWorkflowCatalog(client)
    original = summary("run-a", minutes=1)
    catalog.upsert(original)
    conflicting = replace(
        original,
        state="failed",
        completed_at=original.updated_at,
        node_counts={
            "total": 3,
            "blocked": 0,
            "queued": 0,
            "running": 0,
            "succeeded": 1,
            "failed": 1,
            "canceled": 0,
            "skipped": 1,
        },
    )

    with pytest.raises(ValueError, match="conflicting workflow summary"):
        catalog.upsert(conflicting)

    assert DaprWorkflowCatalog(client).snapshot()[0].state == "running"


class EmptyStatus:
    def get(self, run_id: str, *, actor: str) -> dict[str, object]:
        return {"runId": run_id, "actor": actor}


async def test_fastmcp_list_boundary_supports_all_filters_and_status_fallback() -> None:
    fallback = StatusFlowService(EmptyStatus(), actor="operator")
    application = ListingFlowService(service(), actor="operator", fallback=fallback)
    server = create_server(application)

    _content, structured = await server.call_tool(
        "list_workflows",
        {
            "status": "running",
            "created_after": NOW.isoformat(),
            "updated_before": (NOW + timedelta(days=1)).isoformat(),
            "limit": 2,
        },
    )
    _status_content, status = await server.call_tool(
        "get_workflow_status",
        {"run_id": "run-a"},
    )

    assert [item["runId"] for item in structured["workflows"]] == ["run-a", "run-d"]
    assert status == {"runId": "run-a", "actor": "operator"}
