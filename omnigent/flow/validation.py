"""Pure semantic validation and deterministic execution waves for Flow."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeAlias, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError as PydanticValidationError

from omnigent.flow.contracts import DagSpec, ExpansionRequest, WorkflowNode

Wave: TypeAlias = tuple[str, ...]
Waves: TypeAlias = tuple[Wave, ...]
DispatchBatches: TypeAlias = tuple[tuple[Wave, ...], ...]


@dataclass(frozen=True, slots=True)
class ContractError:
    """One stable, actionable contract violation."""

    code: str
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """A normalized DAG and waves, or deterministic validation errors."""

    dag: DagSpec | None
    errors: tuple[ContractError, ...]
    waves: Waves = ()

    @property
    def is_valid(self) -> bool:
        return not self.errors


_ERROR_ORDER = {
    code: index
    for index, code in enumerate(
        (
            "unsupported_version",
            "empty_nodes",
            "required",
            "invalid_type",
            "invalid_value",
            "invalid_model_reference",
            "non_positive_cap",
            "duplicate_node_id",
            "self_dependency",
            "dangling_dependency",
            "cycle",
            "missing_model",
            "duplicate_tool",
            "invalid_output_schema",
            "max_nodes_exceeded",
            "unknown_proposer",
            "proposer_not_succeeded",
            "proposer_not_expandable",
            "unexpected_round",
            "max_rounds_exceeded",
            "invalid_token_usage",
            "token_budget_exceeded",
        )
    )
}


def validate_dag(value: DagSpec | Mapping[str, Any]) -> ValidationResult:
    """Decode and validate a DAG without calling any external system."""
    if isinstance(value, DagSpec):
        dag = value
    else:
        try:
            dag = DagSpec.model_validate(value)
        except PydanticValidationError as error:
            return ValidationResult(None, _sort_errors(_decode_errors(error)))

    errors = _semantic_errors(dag)
    if errors:
        return ValidationResult(None, errors)
    return ValidationResult(dag, (), execution_waves(dag.nodes))


def validate_expansion(
    dag: DagSpec,
    value: ExpansionRequest | Mapping[str, Any],
    *,
    succeeded_node_ids: Collection[str],
    current_round: int,
    tokens_used: int = 0,
    requested_tokens: int = 0,
) -> ValidationResult:
    """Validate and atomically build the combined DAG for one expansion."""
    if isinstance(value, ExpansionRequest):
        request = value
    else:
        try:
            request = ExpansionRequest.model_validate(value)
        except PydanticValidationError as error:
            return ValidationResult(None, _sort_errors(_decode_errors(error)))

    errors: list[ContractError] = []
    nodes_by_id = {node.id: node for node in dag.nodes}
    proposer = nodes_by_id.get(request.node_id)
    if proposer is None:
        errors.append(
            ContractError(
                "unknown_proposer",
                "/nodeId",
                "nodeId must identify an existing workflow node",
            )
        )
    else:
        if request.node_id not in succeeded_node_ids:
            errors.append(
                ContractError(
                    "proposer_not_succeeded",
                    "/nodeId",
                    "the proposing node must have succeeded",
                )
            )
        if not proposer.can_expand:
            errors.append(
                ContractError(
                    "proposer_not_expandable",
                    "/nodeId",
                    "the proposing node must declare canExpand=true",
                )
            )

    expected_round = current_round + 1
    if request.round != expected_round:
        errors.append(
            ContractError(
                "unexpected_round",
                "/round",
                f"round must be the next round ({expected_round})",
            )
        )
    if request.round > dag.caps.max_rounds:
        errors.append(
            ContractError(
                "max_rounds_exceeded",
                "/round",
                f"round must not exceed caps.maxRounds ({dag.caps.max_rounds})",
            )
        )

    if tokens_used < 0 or requested_tokens < 0:
        errors.append(
            ContractError(
                "invalid_token_usage",
                "/tokenBudget",
                "tokens used and requested tokens must be non-negative",
            )
        )
    elif tokens_used + requested_tokens > dag.caps.token_budget:
        errors.append(
            ContractError(
                "token_budget_exceeded",
                "/tokenBudget",
                "the expansion exceeds the remaining token budget",
            )
        )

    combined = dag.model_copy(update={"nodes": [*dag.nodes, *request.nodes]})
    errors.extend(_semantic_errors(combined))
    ordered = _sort_errors(errors)
    if ordered:
        return ValidationResult(None, ordered)
    return ValidationResult(combined, (), execution_waves(combined.nodes))


def execution_waves(nodes: Sequence[WorkflowNode]) -> Waves:
    """Return stable topological levels using the original node order."""
    if not nodes:
        raise ValueError("at least one node is required")
    remaining = {node.id: set(node.depends_on) for node in nodes}
    ordered_ids = [node.id for node in nodes]
    waves: list[Wave] = []
    completed: set[str] = set()
    while remaining:
        wave = tuple(
            node_id
            for node_id in ordered_ids
            if node_id in remaining and remaining[node_id] <= completed
        )
        if not wave:
            raise ValueError("nodes must form an acyclic graph with valid dependencies")
        waves.append(wave)
        completed.update(wave)
        for node_id in wave:
            del remaining[node_id]
    return tuple(waves)


def dispatch_batches(waves: Waves, *, max_concurrent: int) -> DispatchBatches:
    """Split each dependency wave into deterministic bounded dispatch batches."""
    if max_concurrent <= 0:
        raise ValueError("max_concurrent must be positive")
    return tuple(
        tuple(
            wave[index : index + max_concurrent] for index in range(0, len(wave), max_concurrent)
        )
        for wave in waves
    )


def _semantic_errors(dag: DagSpec) -> tuple[ContractError, ...]:
    errors: list[ContractError] = []
    first_index: dict[str, int] = {}
    for index, node in enumerate(dag.nodes):
        if node.id in first_index:
            errors.append(
                ContractError(
                    "duplicate_node_id",
                    f"/nodes/{index}/id",
                    f"node ID {node.id!r} must be unique",
                )
            )
        else:
            first_index[node.id] = index

    known_ids = set(first_index)
    for node_index, node in enumerate(dag.nodes):
        if node.model is None and dag.default_model is None:
            errors.append(
                ContractError(
                    "missing_model",
                    f"/nodes/{node_index}/model",
                    "set node.model or DagSpec.defaultModel",
                )
            )
        if node.tools is not None:
            duplicate = _first_duplicate(node.tools)
            if duplicate is not None:
                errors.append(
                    ContractError(
                        "duplicate_tool",
                        f"/nodes/{node_index}/tools",
                        f"tool {duplicate!r} may appear only once",
                    )
                )
        if node.output_schema is not None:
            try:
                Draft202012Validator.check_schema(cast(dict[str, Any], node.output_schema))
            except SchemaError as error:
                errors.append(
                    ContractError(
                        "invalid_output_schema",
                        f"/nodes/{node_index}/outputSchema",
                        f"outputSchema is not valid Draft 2020-12: {error.message}",
                    )
                )
        for dependency_index, dependency in enumerate(node.depends_on):
            path = f"/nodes/{node_index}/dependsOn/{dependency_index}"
            if dependency == node.id:
                errors.append(
                    ContractError(
                        "self_dependency",
                        path,
                        "a node cannot depend on itself",
                    )
                )
            elif dependency not in known_ids:
                errors.append(
                    ContractError(
                        "dangling_dependency",
                        path,
                        f"dependency {dependency!r} must identify an existing node",
                    )
                )

    if _has_cycle(dag.nodes):
        errors.append(
            ContractError(
                "cycle",
                "/nodes",
                "dependencies must form an acyclic graph",
            )
        )
    if len(dag.nodes) > dag.caps.max_nodes:
        errors.append(
            ContractError(
                "max_nodes_exceeded",
                "/nodes",
                f"node count must not exceed caps.maxNodes ({dag.caps.max_nodes})",
            )
        )
    return _sort_errors(errors)


def _decode_errors(error: PydanticValidationError) -> tuple[ContractError, ...]:
    errors: list[ContractError] = []
    for detail in error.errors(include_url=False):
        location = detail["loc"]
        path = _json_pointer(location)
        kind = detail["type"]
        if location == ("version",) and kind == "literal_error":
            code = "unsupported_version"
            message = "version must be the supported contract version '1.0'"
        elif location == ("nodes",) and kind == "too_short":
            code = "empty_nodes"
            message = "nodes must contain at least one node"
        elif location and location[0] == "caps" and kind == "greater_than":
            code = "non_positive_cap"
            message = "run caps must be positive integers"
        elif kind == "missing":
            code = "required"
            message = "required field is missing"
        elif kind == "string_pattern_mismatch":
            code = "invalid_model_reference"
            message = "model references must use provider:model form"
        elif kind in {"dict_type", "int_type", "list_type", "string_type", "bool_type"}:
            code = "invalid_type"
            message = detail["msg"]
        else:
            code = "invalid_value"
            message = detail["msg"]
        errors.append(ContractError(code, path, message))
    return tuple(errors)


def _json_pointer(location: Sequence[str | int]) -> str:
    def escape(part: str | int) -> str:
        return str(part).replace("~", "~0").replace("/", "~1")

    return "/" + "/".join(escape(part) for part in location)


def _sort_errors(errors: Collection[ContractError]) -> tuple[ContractError, ...]:
    return tuple(
        sorted(
            set(errors),
            key=lambda error: (
                _ERROR_ORDER.get(error.code, len(_ERROR_ORDER)),
                error.path,
                error.message,
            ),
        )
    )


def _first_duplicate(values: Sequence[str]) -> str | None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            return value
        seen.add(value)
    return None


def _has_cycle(nodes: Sequence[WorkflowNode]) -> bool:
    """Detect cycles among valid non-self edges without masking other errors."""
    by_id: dict[str, WorkflowNode] = {}
    for node in nodes:
        by_id.setdefault(node.id, node)
    dependencies = {
        node_id: {
            dependency
            for dependency in node.depends_on
            if dependency in by_id and dependency != node_id
        }
        for node_id, node in by_id.items()
    }
    remaining = set(by_id)
    completed: set[str] = set()
    while remaining:
        ready = {node_id for node_id in remaining if dependencies[node_id] <= completed}
        if not ready:
            return True
        remaining -= ready
        completed |= ready
    return False
