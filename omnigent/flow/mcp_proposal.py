"""Validated provider-neutral DAG proposal boundary for Flow MCP."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from pydantic import ValidationError

from omnigent.flow.contracts import DagSpec, RunCaps
from omnigent.flow.validation import ContractError, validate_dag

JsonObject = dict[str, object]
_MODEL_REFERENCE = re.compile(r"^[^:\s]+:[^:\s]+$")
_CONSTRAINT_FIELDS = frozenset({"allowedModels", "allowedTools", "caps"})


@dataclass(frozen=True, slots=True)
class ClarificationNeed:
    """One missing value that must be supplied before a safe DAG can be proposed."""

    field: str
    question: str
    reason: str


@dataclass(frozen=True, slots=True)
class ProposalRequest:
    """Language-neutral request passed to an injected proposal generator."""

    task_description: str
    allowed_models: tuple[str, ...] | None = None
    allowed_tools: tuple[str, ...] | None = None
    caps: RunCaps | None = None


@dataclass(frozen=True, slots=True)
class ProposalDraft:
    """Provider-neutral model result before canonical validation."""

    dag_spec: Mapping[str, Any] | None = None
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    clarification_needs: tuple[ClarificationNeed, ...] = ()


class ProposalGenerator(Protocol):
    """Model/provider-independent proposal generation boundary."""

    async def propose(self, request: ProposalRequest) -> ProposalDraft: ...


class ProposalGenerationError(Exception):
    """Provider-neutral proposal failure whose details must stay internal."""


class FallbackFlowService(Protocol):
    async def run_workflow(
        self,
        dag_spec: JsonObject,
        approval_token: str | None = None,
        confirm: bool = False,
        idempotency_key: str | None = None,
    ) -> JsonObject: ...

    async def get_workflow_status(self, run_id: str) -> JsonObject: ...

    async def list_workflows(
        self,
        status: str | None,
        cursor: str | None,
        limit: int,
        created_after: str | None = None,
        created_before: str | None = None,
        updated_after: str | None = None,
        updated_before: str | None = None,
    ) -> JsonObject: ...


class DagProposalService:
    """Generate and validate candidate DAGs without approving or starting runs."""

    def __init__(
        self,
        generator: ProposalGenerator,
        *,
        fallback: FallbackFlowService,
    ) -> None:
        self._generator = generator
        self._fallback = fallback

    async def propose_dag(
        self,
        task_description: str,
        constraints: JsonObject | None = None,
    ) -> JsonObject:
        try:
            request = _proposal_request(task_description, constraints)
        except ValueError as error:
            return _error("invalid_input", str(error))

        try:
            draft = await self._generator.propose(request)
        except ProposalGenerationError:
            return _error("proposal_failed", "proposal generation failed safely")
        if draft.clarification_needs:
            return {
                "status": "clarification_required",
                "clarificationNeeds": [
                    {
                        "field": need.field,
                        "question": need.question,
                        "reason": need.reason,
                    }
                    for need in draft.clarification_needs
                ],
                "assumptions": list(draft.assumptions),
                "warnings": list(draft.warnings),
            }
        if draft.dag_spec is None:
            return _error(
                "invalid_proposal",
                "proposal generator returned neither a DAG nor clarification needs",
            )

        validation = validate_dag(draft.dag_spec)
        if validation.dag is None:
            return _invalid_proposal("invalid_proposal", validation.errors)
        constraint_errors = _constraint_errors(validation.dag, request)
        if constraint_errors:
            return _invalid_proposal("proposal_constraint_violation", constraint_errors)
        return {
            "status": "proposed",
            "dagSpec": validation.dag.model_dump(mode="json", by_alias=True),
            "validation": {"valid": True, "errors": []},
            "assumptions": list(draft.assumptions),
            "warnings": list(draft.warnings),
        }

    async def run_workflow(
        self,
        dag_spec: JsonObject,
        approval_token: str | None = None,
        confirm: bool = False,
        idempotency_key: str | None = None,
    ) -> JsonObject:
        return await self._fallback.run_workflow(
            dag_spec,
            approval_token,
            confirm,
            idempotency_key,
        )

    async def get_workflow_status(self, run_id: str) -> JsonObject:
        return await self._fallback.get_workflow_status(run_id)

    async def list_workflows(
        self,
        status: str | None,
        cursor: str | None,
        limit: int,
        created_after: str | None = None,
        created_before: str | None = None,
        updated_after: str | None = None,
        updated_before: str | None = None,
    ) -> JsonObject:
        return await self._fallback.list_workflows(
            status,
            cursor,
            limit,
            created_after,
            created_before,
            updated_after,
            updated_before,
        )


def _proposal_request(
    task_description: str,
    constraints: JsonObject | None,
) -> ProposalRequest:
    if not task_description.strip():
        raise ValueError("task_description cannot be blank")
    values = constraints or {}
    unsupported = sorted(set(values) - _CONSTRAINT_FIELDS)
    if unsupported:
        raise ValueError(f"unsupported constraint fields: {', '.join(unsupported)}")
    allowed_models = _string_tuple(values.get("allowedModels"), "allowedModels")
    if allowed_models is not None:
        if not allowed_models:
            raise ValueError("allowedModels cannot be empty")
        if any(_MODEL_REFERENCE.fullmatch(model) is None for model in allowed_models):
            raise ValueError("allowedModels entries must use provider:model")
    allowed_tools = _string_tuple(values.get("allowedTools"), "allowedTools")
    caps_value = values.get("caps")
    try:
        caps = None if caps_value is None else RunCaps.model_validate(caps_value)
    except ValidationError as error:
        raise ValueError("caps must contain positive canonical cap fields") from error
    return ProposalRequest(
        task_description=task_description.strip(),
        allowed_models=allowed_models,
        allowed_tools=allowed_tools,
        caps=caps,
    )


def _string_tuple(value: object, field: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{field} must be an array of non-empty strings")
    normalized = tuple(cast(str, item).strip() for item in value)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} cannot contain duplicates")
    return normalized


def _constraint_errors(
    dag: DagSpec,
    request: ProposalRequest,
) -> tuple[ContractError, ...]:
    errors: list[ContractError] = []
    allowed_models = set(request.allowed_models or ())
    allowed_tools = set(request.allowed_tools or ())
    for index, node in enumerate(dag.nodes):
        selected_model = node.model or dag.default_model
        if request.allowed_models is not None and selected_model not in allowed_models:
            errors.append(
                ContractError(
                    "model_not_allowed",
                    f"/nodes/{index}/model",
                    "proposed model is outside allowedModels",
                )
            )
        if request.allowed_tools is not None:
            for tool_index, tool in enumerate(node.tools or ()):
                if tool not in allowed_tools:
                    errors.append(
                        ContractError(
                            "tool_not_allowed",
                            f"/nodes/{index}/tools/{tool_index}",
                            "proposed tool is outside allowedTools",
                        )
                    )
    if request.caps is not None:
        for field_name, alias in (
            ("max_nodes", "maxNodes"),
            ("max_rounds", "maxRounds"),
            ("max_concurrent", "maxConcurrent"),
            ("token_budget", "tokenBudget"),
        ):
            if getattr(dag.caps, field_name) > getattr(request.caps, field_name):
                errors.append(
                    ContractError(
                        "cap_exceeded",
                        f"/caps/{alias}",
                        f"proposed {alias} exceeds the requested ceiling",
                    )
                )
    return tuple(errors)


def _invalid_proposal(
    code: str,
    errors: tuple[ContractError, ...],
) -> JsonObject:
    return {
        "error": {"code": code, "message": "proposal did not satisfy its contract"},
        "validation": {
            "valid": False,
            "errors": [
                {"code": error.code, "path": error.path, "message": error.message}
                for error in errors
            ],
        },
    }


def _error(code: str, message: str) -> JsonObject:
    return {"error": {"code": code, "message": message}}
