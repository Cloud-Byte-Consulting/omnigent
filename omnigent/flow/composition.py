"""Validated production composition root for the Flow MCP entrypoint."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.error import URLError
from urllib.request import urlopen
from uuid import NAMESPACE_URL, uuid4, uuid5

from dapr.clients import DaprClient
from dapr.ext.workflow import DaprWorkflowClient

from omnigent.flow.approval import ApprovalRecord, ApprovalService, SQLiteApprovalStore
from omnigent.flow.audit import DaprAuditStore
from omnigent.flow.caps import DaprCapStore
from omnigent.flow.listing import (
    DaprWorkflowCatalog,
    WorkflowListingService,
    WorkflowSummary,
)
from omnigent.flow.mcp_listing import ListingFlowService
from omnigent.flow.mcp_proposal import (
    ClarificationNeed,
    DagProposalService,
    ProposalDraft,
    ProposalGenerator,
    ProposalRequest,
)
from omnigent.flow.mcp_run import ApprovedDaprWorkflowStarter, WorkflowRunFlowService
from omnigent.flow.mcp_server import FlowService
from omnigent.flow.mcp_status import StatusFlowService
from omnigent.flow.status import WorkflowStatusService
from omnigent.flow.usage import ConservativeUsagePolicy, DaprUsageStore, UsageService


class Closable(Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class FlowApplicationConfig:
    """Secret-safe environment configuration required by the stdio process."""

    mode: str
    actor: str
    signing_key: bytes
    approval_database: Path
    approval_ttl: timedelta
    dapr_grpc_port: int
    dapr_http_port: int
    dapr_health_timeout_seconds: float = 2.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> FlowApplicationConfig:
        mode = _required(env, "FLOW_MODE")
        if mode != "conformance":
            raise ValueError("FLOW_MODE must select the configured conformance composition")
        actor = _required(env, "FLOW_ACTOR")
        signing_key = _required(env, "FLOW_SIGNING_KEY").encode()
        if len(signing_key) < 16:
            raise ValueError("FLOW_SIGNING_KEY must contain at least 16 bytes")
        database = Path(_required(env, "FLOW_APPROVAL_DB")).expanduser()
        ttl_seconds = _positive_integer(env, "FLOW_APPROVAL_TTL_SECONDS")
        return cls(
            mode=mode,
            actor=actor,
            signing_key=signing_key,
            approval_database=database,
            approval_ttl=timedelta(seconds=ttl_seconds),
            dapr_grpc_port=_tcp_port(env, "DAPR_GRPC_PORT"),
            dapr_http_port=_tcp_port(env, "DAPR_HTTP_PORT"),
            dapr_health_timeout_seconds=_positive_number(
                env, "FLOW_DAPR_HEALTH_TIMEOUT_SECONDS", default=2.0
            ),
        )


@dataclass(slots=True)
class FlowApplication:
    """Fully composed service plus deterministic client lifecycle ownership."""

    service: FlowService
    _clients: tuple[Closable, ...]
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _close_clients(self._clients)


class DeterministicProposalGenerator:
    """Credential-free provider-neutral generator for explicit conformance mode."""

    async def propose(self, request: ProposalRequest) -> ProposalDraft:
        model = "fake:deterministic"
        if request.allowed_models is not None and model not in request.allowed_models:
            return ProposalDraft(
                clarification_needs=(
                    ClarificationNeed(
                        field="allowedModels",
                        question="Can this conformance run use fake:deterministic?",
                        reason=(
                            "Conformance mode exposes only the credential-free "
                            "fake:deterministic route."
                        ),
                    ),
                )
            )
        if request.allowed_tools:
            return ProposalDraft(
                clarification_needs=(
                    ClarificationNeed(
                        field="allowedTools",
                        question="Can this conformance run proceed without tools?",
                        reason=(
                            "The credential-free fake:deterministic route does not execute tools."
                        ),
                    ),
                )
            )
        caps = (
            request.caps.model_dump(mode="json", by_alias=True)
            if request.caps is not None
            else {
                "maxNodes": 3,
                "maxRounds": 1,
                "maxConcurrent": 2,
                "tokenBudget": 3,
            }
        )
        return ProposalDraft(
            dag_spec={
                "version": "1.0",
                "nodes": [
                    {
                        "id": "A",
                        "instructions": f"First branch for: {request.task_description}",
                        "model": model,
                    },
                    {
                        "id": "B",
                        "instructions": f"Second branch for: {request.task_description}",
                        "model": model,
                    },
                    {
                        "id": "C",
                        "instructions": "Join the two branch results",
                        "dependsOn": ["A", "B"],
                        "model": model,
                    },
                ],
                "caps": caps,
            },
            assumptions=("Conformance mode uses the deterministic local adapter.",),
        )


class _CatalogingStarter:
    def __init__(
        self,
        workflow_client: Any,
        catalog: DaprWorkflowCatalog,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._workflow_client = workflow_client
        self._start = ApprovedDaprWorkflowStarter(workflow_client)
        self._catalog = catalog
        self._clock = clock

    def __call__(self, run_id: str, record: ApprovalRecord) -> None:
        if self._workflow_client.get_workflow_state(run_id) is None:
            self._start(run_id, record)
        now = self._clock()
        total = len(record.dag_snapshot.get("nodes", []))
        self._catalog.upsert(
            WorkflowSummary(
                run_id=run_id,
                dag_digest=record.dag_digest,
                dag_name=None,
                state="queued",
                created_at=now,
                updated_at=now,
                completed_at=None,
                node_counts={
                    "total": total,
                    "blocked": 0,
                    "queued": total,
                    "running": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "canceled": 0,
                    "skipped": 0,
                },
            )
        )


class _RefreshingCatalog:
    """Refresh durable safe summaries from canonical Dapr status on each listing."""

    def __init__(
        self,
        catalog: DaprWorkflowCatalog,
        status: WorkflowStatusService,
        *,
        actor: str,
    ) -> None:
        self._catalog = catalog
        self._status = status
        self._actor = actor

    def snapshot(self) -> Sequence[WorkflowSummary]:
        refreshed: list[WorkflowSummary] = []
        for record in self._catalog.snapshot():
            status = self._status.get(record.run_id, actor=self._actor)
            state = status.get("state")
            nodes = status.get("nodes")
            timestamps = status.get("timestamps")
            if not isinstance(state, str) or not isinstance(nodes, Mapping):
                refreshed.append(record)
                continue
            if not isinstance(timestamps, Mapping):
                timestamps = {}
            counts = {key: 0 for key in record.node_counts if key != "total"}
            for node in nodes.values():
                if not isinstance(node, Mapping):
                    continue
                node_state = node.get("state")
                if node_state == "pending":
                    node_state = "queued"
                if isinstance(node_state, str) and node_state in counts:
                    counts[node_state] += 1
            counts["total"] = len(nodes)
            updated_at = _timestamp(timestamps.get("updatedAt")) or record.updated_at
            completed_at = _timestamp(timestamps.get("completedAt"))
            unchanged = (
                state == record.state
                and counts == record.node_counts
                and completed_at == record.completed_at
            )
            if updated_at <= record.updated_at:
                updated_at = (
                    record.updated_at
                    if unchanged
                    else record.updated_at + timedelta(microseconds=1)
                )
            candidate = replace(
                record,
                state=cast(Any, state),
                updated_at=updated_at,
                completed_at=completed_at,
                node_counts=counts,
            )
            refreshed.append(self._catalog.upsert(candidate))
        return tuple(refreshed)


def build_flow_application(
    config: FlowApplicationConfig,
    *,
    state_client: Any | None = None,
    workflow_client: Any | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    id_factory: Callable[[], str] = lambda: str(uuid4()),
    proposal_generator: ProposalGenerator | None = None,
) -> FlowApplication:
    """Compose every canonical MCP operation from durable production services."""
    if config.mode != "conformance" and proposal_generator is None:
        raise ValueError("a proposal generator is required outside conformance mode")
    config.approval_database.parent.mkdir(parents=True, exist_ok=True)
    clients: list[Closable] = []
    try:
        if state_client is None or workflow_client is None:
            _require_dapr_ready(config)
        state: Any = state_client or DaprClient(address=f"127.0.0.1:{config.dapr_grpc_port}")
        clients.append(cast(Closable, state))
        workflow: Any = workflow_client or DaprWorkflowClient(
            host="127.0.0.1", port=str(config.dapr_grpc_port)
        )
        clients.append(cast(Closable, workflow))
        audit = DaprAuditStore(state)
        usage = UsageService(
            DaprUsageStore(state),
            missing_usage_policy=ConservativeUsagePolicy(1),
        )
        caps = DaprCapStore(state)
        catalog = DaprWorkflowCatalog(state)

        def authorizer(actor: str, run_id: str) -> bool:
            return actor == config.actor and bool(run_id)

        status_reader = WorkflowStatusService(
            workflow,
            audit=audit,
            usage=usage,
            caps=caps,
            authorizer=authorizer,
        )
        status = StatusFlowService(status_reader, actor=config.actor)
        listing = ListingFlowService(
            WorkflowListingService(
                _RefreshingCatalog(catalog, status_reader, actor=config.actor),
                authorizer=authorizer,
            ),
            actor=config.actor,
            fallback=status,
        )
        approvals = ApprovalService(
            SQLiteApprovalStore(config.approval_database),
            signing_key=config.signing_key,
            start_run=_CatalogingStarter(workflow, catalog, clock=clock),
            id_factory=id_factory,
            run_id_factory=lambda record: str(
                uuid5(NAMESPACE_URL, f"flow-approval:{record.approval_id}")
            ),
            audit=audit,
        )
        run = WorkflowRunFlowService(
            approvals,
            actor=config.actor,
            clock=clock,
            approval_ttl=config.approval_ttl,
            fallback=listing,
        )
        generator = proposal_generator or DeterministicProposalGenerator()
        service = DagProposalService(generator, fallback=run)
        return FlowApplication(service, tuple(clients))
    except Exception:
        _close_clients(clients)
        raise


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _positive_integer(env: Mapping[str, str], name: str) -> int:
    value = _required(env, name)
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _tcp_port(env: Mapping[str, str], name: str) -> int:
    value = _required(env, name)
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a TCP port") from error
    if parsed < 1 or parsed > 65535:
        raise ValueError(f"{name} must be a TCP port")
    return parsed


def _positive_number(
    env: Mapping[str, str],
    name: str,
    *,
    default: float,
) -> float:
    value = env.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive number") from error
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive number")
    return parsed


def _require_dapr_ready(config: FlowApplicationConfig) -> None:
    url = f"http://127.0.0.1:{config.dapr_http_port}/v1.0/healthz/outbound"
    try:
        with urlopen(url, timeout=config.dapr_health_timeout_seconds) as response:
            if response.status < 200 or response.status >= 300:
                raise ValueError("Dapr endpoint is unavailable")
    except (OSError, TimeoutError, URLError) as error:
        raise ValueError("Dapr endpoint is unavailable") from error


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _close_clients(clients: Sequence[Closable]) -> None:
    for client in reversed(clients):
        try:
            client.close()
        except (OSError, RuntimeError):
            continue
