"""Python models and language-neutral JSON Schemas for Flow."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
ModelReference = Annotated[
    str,
    StringConstraints(pattern=r"^[^:\s]+:[^:\s]+$"),
]
SchemaName = Literal["dag-spec", "expansion-request"]
JsonObject = dict[str, object]


def _camel_case(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class _ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=lambda name: _camel_case(name),
        extra="forbid",
        strict=True,
    )


class RunCaps(_ContractModel):
    """Hard limits applied to one workflow run."""

    max_nodes: int = Field(gt=0, description="Maximum nodes accepted across all expansion rounds.")
    max_rounds: int = Field(gt=0, description="Maximum dynamic-expansion rounds.")
    max_concurrent: int = Field(gt=0, description="Maximum nodes that may execute concurrently.")
    token_budget: int = Field(gt=0, description="Maximum aggregate model tokens for the run.")


class WorkflowNode(_ContractModel):
    """One provider-neutral unit of work in a Flow DAG."""

    id: NonEmptyString = Field(description="Unique node identifier within the workflow.")
    instructions: NonEmptyString = Field(description="Task instructions for this node.")
    depends_on: list[NonEmptyString] = Field(
        default_factory=list,
        description="Node IDs that must succeed before this node may run.",
    )
    model: ModelReference | None = Field(
        default=None,
        description="Optional provider:model override.",
    )
    tools: list[NonEmptyString] | None = Field(
        default=None,
        description="Optional allowlist of tool names available to the node.",
    )
    output_schema: JsonObject | None = Field(
        default=None,
        description="Optional JSON Schema for the node result.",
    )
    can_expand: bool = Field(
        default=False,
        description="Whether a successful node may propose a bounded expansion.",
    )


class DagSpec(_ContractModel):
    """Versioned, provider-neutral workflow definition."""

    model_config = ConfigDict(
        alias_generator=lambda name: _camel_case(name),
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$id": "https://cloudbyteconsulting.com/schemas/flow/dag-spec/1.0.json",
            "$schema": "https://json-schema.org/draft/2020-12/schema",
        },
    )

    version: Literal["1.0"] = Field(description="Flow contract version.")
    nodes: list[WorkflowNode] = Field(min_length=1, description="Non-empty workflow node list.")
    default_model: ModelReference | None = Field(
        default=None,
        description="Optional provider:model used when a node has no override.",
    )
    caps: RunCaps


class ExpansionRequest(_ContractModel):
    """Atomic proposal to add nodes in the next workflow round."""

    model_config = ConfigDict(
        alias_generator=lambda name: _camel_case(name),
        extra="forbid",
        strict=True,
        json_schema_extra={
            "$id": "https://cloudbyteconsulting.com/schemas/flow/expansion-request/1.0.json",
            "$schema": "https://json-schema.org/draft/2020-12/schema",
        },
    )

    node_id: NonEmptyString = Field(description="ID of the successful node proposing expansion.")
    round: int = Field(gt=0, description="Expected next expansion round.")
    nodes: list[WorkflowNode] = Field(min_length=1, description="Nodes proposed atomically.")


def generated_schema(name: SchemaName) -> JsonObject:
    """Return the canonical schema produced by the Python contract models."""
    model = DagSpec if name == "dag-spec" else ExpansionRequest
    return cast(JsonObject, model.model_json_schema(by_alias=True, mode="validation"))


def published_schema(name: SchemaName) -> JsonObject:
    """Load a checked-in, versioned public schema."""
    schema_path = files("omnigent.flow.schemas").joinpath(f"{name}-1.0.json")
    return cast(JsonObject, json.loads(schema_path.read_text(encoding="utf-8")))
