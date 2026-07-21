import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from omnigent.flow.contracts import (
    DagSpec,
    ExpansionRequest,
    generated_schema,
    published_schema,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def test_validate_canonical_positive_fixture() -> None:
    dag = _load(FIXTURES / "positive" / "dag-spec.json")
    schema = published_schema("dag-spec")

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(dag)


@pytest.mark.parametrize(
    ("name", "expected_path"),
    [
        ("missing-version.json", []),
        ("invalid-node-type.json", ["nodes"]),
        ("empty-nodes.json", ["nodes"]),
        ("non-positive-cap.json", ["caps", "maxConcurrent"]),
        ("invalid-model-reference.json", ["nodes", 0, "model"]),
    ],
)
def test_reject_canonical_negative_fixture(name: str, expected_path: list[str | int]) -> None:
    fixture = _load(FIXTURES / "negative" / name)

    errors = list(Draft202012Validator(published_schema("dag-spec")).iter_errors(fixture))

    assert errors
    assert list(errors[0].absolute_path) == expected_path


def test_schema_and_python_validation_agree() -> None:
    fixtures = [
        FIXTURES / "positive" / "dag-spec.json",
        *sorted((FIXTURES / "negative").glob("*.json")),
    ]
    schema_validator = Draft202012Validator(published_schema("dag-spec"))

    for path in fixtures:
        fixture = _load(path)
        schema_accepts = schema_validator.is_valid(fixture)
        python_accepts = True
        try:
            DagSpec.model_validate(fixture)
        except ValueError:
            python_accepts = False
        assert python_accepts == schema_accepts, path.name


def test_validate_expansion_request_fixture() -> None:
    expansion = _load(FIXTURES / "positive" / "expansion-request.json")

    Draft202012Validator(published_schema("expansion-request")).validate(expansion)
    assert ExpansionRequest.model_validate(expansion).round == 2


def test_published_schema_is_language_neutral() -> None:
    encoded = json.dumps(published_schema("dag-spec")).lower()

    runtime_names = ("pydantic", "python", "typescript", ".net", "java")
    assert all(name not in encoded for name in runtime_names)


@pytest.mark.parametrize("name", ["dag-spec", "expansion-request"])
def test_checked_in_schema_matches_python_contract(name: str) -> None:
    assert published_schema(name) == generated_schema(name)
