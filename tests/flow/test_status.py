import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from dapr.ext.workflow.workflow_state import WorkflowStatus

from omnigent.flow.audit import InMemoryAuditStore, create_audit_event
from omnigent.flow.caps import CapProposal, InMemoryCapStore
from omnigent.flow.contracts import RunCaps
from omnigent.flow.providers import TokenUsage
from omnigent.flow.status import WorkflowStatusService
from omnigent.flow.usage import (
    ConservativeUsagePolicy,
    InMemoryUsageStore,
    UsageService,
)

NOW = datetime(2026, 7, 21, tzinfo=UTC)


@dataclass
class DaprState:
    runtime_status: WorkflowStatus
    created_at: datetime
    last_updated_at: datetime
    serialized_input: str
    serialized_output: str | None
    serialized_custom_status: str | None


class FakeClient:
    def __init__(self, state: DaprState | None) -> None:
        self.state = state
        self.queries: list[str] = []

    def get_workflow_state(self, instance_id: str) -> DaprState | None:
        self.queries.append(instance_id)
        return self.state


def dag_input() -> dict:
    return {
        "runId": "run-1",
        "approvedDagDigest": "approved-digest",
        "dagSpec": {
            "version": "1.0",
            "defaultModel": "fake:alpha",
            "nodes": [
                {"id": "A", "instructions": "A"},
                {"id": "B", "instructions": "B"},
                {"id": "C", "instructions": "C"},
                {"id": "D", "instructions": "D", "dependsOn": ["A"]},
            ],
            "caps": {
                "maxNodes": 4,
                "maxRounds": 2,
                "maxConcurrent": 2,
                "tokenBudget": 100,
            },
        },
        "persistedResults": {},
    }


def dapr_state(
    nodes: dict[str, dict],
    *,
    runtime_status: WorkflowStatus = WorkflowStatus.RUNNING,
    output: dict | None = None,
) -> DaprState:
    return DaprState(
        runtime_status=runtime_status,
        created_at=NOW,
        last_updated_at=NOW + timedelta(seconds=5),
        serialized_input=json.dumps(dag_input()),
        serialized_output=None if output is None else json.dumps(output),
        serialized_custom_status=json.dumps(
            {
                "runId": "run-1",
                "approvedDagDigest": "approved-digest",
                "status": "running",
                "nodes": nodes,
                "events": [],
            }
        ),
    )


def add_event(
    store: InMemoryAuditStore,
    event_type: str,
    *,
    sequence: int,
    node_id: str | None = None,
    metadata: dict | None = None,
    summary: str | None = None,
) -> None:
    store.append(
        create_audit_event(
            run_id="run-1",
            node_id=node_id,
            event_type=event_type,
            timestamp=NOW + timedelta(seconds=sequence),
            source="test",
            correlation_key=f"{event_type}:{sequence}",
            summary=summary or event_type,
            metadata=metadata or {},
        )
    )


def setup_service(
    state: DaprState | None,
    *,
    authorized: bool = True,
) -> tuple[WorkflowStatusService, InMemoryAuditStore, UsageService, InMemoryCapStore]:
    audit = InMemoryAuditStore()
    usage = UsageService(
        InMemoryUsageStore(),
        missing_usage_policy=ConservativeUsagePolicy(20),
    )
    cap_store = InMemoryCapStore()
    return (
        WorkflowStatusService(
            FakeClient(state),
            audit=audit,
            usage=usage,
            caps=cap_store,
            authorizer=lambda actor, run_id: authorized
            and actor == "operator"
            and run_id == "run-1",
        ),
        audit,
        usage,
        cap_store,
    )


def initialize_caps(cap_store: InMemoryCapStore) -> RunCaps:
    limits = RunCaps.model_validate(dag_input()["dagSpec"]["caps"])
    cap_store.apply(
        "run-1",
        limits,
        CapProposal.accept_nodes("round-1", ("A", "B", "C", "D"), round_number=1),
        used_tokens=0,
    )
    cap_store.apply(
        "run-1",
        limits,
        CapProposal.dispatch("dispatch-A", "A", required_tokens=20),
        used_tokens=0,
    )
    cap_store.apply(
        "run-1",
        limits,
        CapProposal.dispatch("dispatch-B", "B", required_tokens=20),
        used_tokens=0,
    )
    cap_store.apply(
        "run-1",
        limits,
        CapProposal.dispatch("dispatch-C", "C", required_tokens=20),
        used_tokens=0,
    )
    cap_store.apply(
        "run-1",
        limits,
        CapProposal.complete("complete-A", "A"),
        used_tokens=0,
    )
    return limits


def test_active_run_is_self_contained_with_nodes_caps_models_and_timestamps() -> None:
    nodes = {
        "A": {"status": "succeeded"},
        "B": {"status": "running", "attempt": 2},
        "C": {"status": "queued"},
        "D": {"status": "blocked", "blockedBy": ["A"]},
    }
    status, audit, usage, cap_store = setup_service(dapr_state(nodes))
    limits = initialize_caps(cap_store)
    usage.record_attempt(
        run_id="run-1",
        idempotency_key="A:attempt:1",
        node_id="A",
        attempt=1,
        provider="fake",
        model="alpha",
        succeeded=True,
        usage=TokenUsage(input_tokens=6, output_tokens=4, total_tokens=10),
        token_budget=limits.token_budget,
    )
    add_event(
        audit,
        "approval",
        sequence=1,
        metadata={"decision": "approved", "approver": "ops", "token": "secret"},
    )
    add_event(audit, "run_running", sequence=2)
    add_event(audit, "node_succeeded", sequence=3, node_id="A", metadata={"attempt": 1})
    add_event(audit, "node_running", sequence=4, node_id="B", metadata={"attempt": 2})
    add_event(audit, "node_queued", sequence=5, node_id="C")
    add_event(audit, "node_blocked", sequence=6, node_id="D", metadata={"blockedBy": ["A"]})

    result = status.get("run-1", actor="operator")

    assert result["state"] == "running"
    assert result["dag"] == {"digest": "approved-digest", "version": "1.0"}
    assert result["defaultModel"] == "fake:alpha"
    assert result["caps"]["limits"] == dag_input()["dagSpec"]["caps"]
    assert result["caps"]["utilization"]["acceptedNodes"] == 4
    assert result["caps"]["utilization"]["usedTokens"] == 10
    assert result["caps"]["utilization"]["reservedTokens"] == 20
    assert result["caps"]["utilization"]["availableTokens"] == 70
    assert result["nodes"]["A"]["usage"]["totalTokens"] == 10
    assert result["nodes"]["B"]["attempts"] == 2
    assert result["nodes"]["D"]["blockedBy"] == ["A"]
    assert result["approval"] == {
        "approved": True,
        "decision": "approved",
        "approver": "ops",
        "decidedAt": (NOW + timedelta(seconds=1)).isoformat(),
    }
    assert "token" not in result["approval"]
    assert result["timestamps"]["startedAt"] == (NOW + timedelta(seconds=2)).isoformat()
    json.dumps(result)


def test_failed_run_exposes_only_safe_failure_and_failed_dependency() -> None:
    nodes = {
        "A": {
            "status": "failed",
            "failure": {
                "category": "permanent",
                "retryable": False,
                "message": "safe failure",
                "rawPayload": "sk-super-secret",
            },
        },
        "B": {"status": "blocked", "blockedBy": ["A"]},
        "C": {"status": "skipped"},
        "D": {"status": "skipped"},
    }
    terminal = {"status": "failed", "nodes": nodes}
    status, audit, _usage, cap_store = setup_service(
        dapr_state(nodes, runtime_status=WorkflowStatus.COMPLETED, output=terminal)
    )
    initialize_caps(cap_store)
    add_event(audit, "node_failed", sequence=1, node_id="A", summary="safe failure")
    add_event(audit, "run_failed", sequence=2, summary="retry policy exhausted")

    result = status.get("run-1", actor="operator")

    assert result["state"] == "failed"
    assert result["nodes"]["A"]["failure"] == {
        "category": "permanent",
        "retryable": False,
        "message": "safe failure",
        "policyExhausted": True,
    }
    assert result["nodes"]["B"]["blockedBy"] == ["A"]
    assert result["interventionReason"] == "retry policy exhausted"
    assert "sk-super-secret" not in json.dumps(result)
    assert result["redaction"]["rawProviderPayloadsExcluded"] is True


def test_expansion_decisions_are_in_order_with_cap_reason() -> None:
    status, audit, _usage, cap_store = setup_service(dapr_state({}))
    initialize_caps(cap_store)
    add_event(audit, "expansion", sequence=1, metadata={"round": 2, "decision": "accepted"})
    add_event(
        audit,
        "cap_denial",
        sequence=2,
        metadata={"round": 3, "cap": "maxRounds", "decision": "rejected"},
    )

    result = status.get("run-1", actor="operator")

    assert [(item["sequence"], item["type"]) for item in result["expansionHistory"]] == [
        (1, "expansion"),
        (2, "cap_denial"),
    ]
    assert result["expansionHistory"][1]["metadata"]["cap"] == "maxRounds"


def test_missing_and_unauthorized_runs_are_indistinguishable() -> None:
    missing, *_ = setup_service(None)
    unauthorized, *_ = setup_service(dapr_state({}), authorized=False)

    assert missing.get("run-1", actor="operator") == {
        "runId": "run-1",
        "error": "not_found",
    }
    assert unauthorized.get("run-1", actor="intruder") == {
        "runId": "run-1",
        "error": "not_found",
    }
