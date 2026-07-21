import json
import socket
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from omnigent.flow.contracts import DagSpec
from omnigent.flow.validation import dispatch_batches, validate_dag, validate_expansion

FIXTURE = Path(__file__).parent / "fixtures" / "contracts" / "manifest.json"
DEFAULT_CAPS = {
    "maxNodes": 3,
    "maxRounds": 2,
    "maxConcurrent": 2,
    "tokenBudget": 100,
}


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _dag(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": case.get("version", "1.0"),
        "nodes": case["nodes"],
        "defaultModel": case.get("defaultModel", "fake:test"),
        "caps": case.get("caps", DEFAULT_CAPS),
    }


@pytest.mark.parametrize(
    "name",
    [
        "valid-single-node",
        "valid-fan-out-fan-in",
        "valid-chain",
        "valid-disconnected-components",
    ],
)
def test_valid_fixtures_produce_deterministic_waves(
    manifest: dict[str, Any],
    name: str,
) -> None:
    case = manifest["dagCases"][name]

    first = validate_dag(_dag(case))
    second = validate_dag(_dag(case))

    assert first.errors == ()
    assert first.waves == tuple(tuple(wave) for wave in case["expectedWaves"])
    assert first == second


def test_dispatch_batches_limit_concurrency_without_changing_waves(
    manifest: dict[str, Any],
) -> None:
    case = manifest["dagCases"]["valid-disconnected-components"]
    result = validate_dag(_dag(case))

    batches = dispatch_batches(result.waves, max_concurrent=1)

    assert batches == ((("A",), ("X",)), (("B",), ("Y",)))
    assert tuple(node for wave in batches for batch in wave for node in batch) == (
        "A",
        "X",
        "B",
        "Y",
    )


@pytest.mark.parametrize(
    "name",
    [
        "invalid-unsupported-version",
        "invalid-empty-nodes",
        "invalid-duplicate-ids",
        "invalid-self-reference",
        "invalid-dangling-dependency",
        "invalid-direct-cycle",
        "invalid-indirect-cycle",
        "invalid-non-positive-cap",
        "invalid-node-overflow",
        "invalid-output-schema",
        "invalid-missing-model",
        "invalid-tool-allowlist",
        "invalid-multiple-independent-errors",
    ],
)
def test_invalid_fixtures_return_stable_actionable_errors(
    manifest: dict[str, Any],
    name: str,
) -> None:
    case = manifest["dagCases"][name]

    first = validate_dag(_dag(case))
    second = validate_dag(_dag(case))

    assert [error.code for error in first.errors] == case["expectedErrors"]
    assert [error.path for error in first.errors] == case["expectedPaths"]
    assert all(error.message for error in first.errors)
    assert first == second
    assert first.waves == ()


@pytest.mark.parametrize(
    "name",
    [
        "valid-next-round",
        "invalid-proposer",
        "invalid-proposer-not-succeeded",
        "invalid-proposer-cannot-expand",
        "invalid-unexpected-round",
        "invalid-duplicate-existing-id",
        "invalid-duplicate-proposed-ids",
        "invalid-expansion-cycle",
        "invalid-expansion-dangling-dependency",
        "invalid-expansion-cycle-and-dangling",
        "invalid-expansion-node-overflow",
        "invalid-expansion-token-budget",
    ],
)
def test_expansion_fixtures_are_atomic(
    manifest: dict[str, Any],
    name: str,
) -> None:
    case = manifest["expansionCases"][name]
    base = DagSpec.model_validate(
        {
            "version": "1.0",
            "defaultModel": "fake:test",
            "nodes": [
                {
                    "id": "A",
                    "instructions": "Run A",
                    "model": "fake:test",
                    "canExpand": case.get("proposerCanExpand", True),
                }
            ],
            "caps": DEFAULT_CAPS,
        }
    )
    before = base.model_dump(mode="json", by_alias=True)

    result = validate_expansion(
        base,
        case["request"],
        succeeded_node_ids=case["succeededNodeIds"],
        current_round=case["currentRound"],
        tokens_used=case.get("tokensUsed", 0),
        requested_tokens=case.get("requestedTokens", 0),
    )
    repeated = validate_expansion(
        base,
        case["request"],
        succeeded_node_ids=case["succeededNodeIds"],
        current_round=case["currentRound"],
        tokens_used=case.get("tokensUsed", 0),
        requested_tokens=case.get("requestedTokens", 0),
    )

    assert [error.code for error in result.errors] == case["expectedErrors"]
    assert [error.path for error in result.errors] == case.get("expectedPaths", [])
    assert all(error.message for error in result.errors)
    assert result == repeated
    assert base.model_dump(mode="json", by_alias=True) == before
    if result.errors:
        assert result.dag is None
        assert result.waves == ()
    else:
        assert result.dag is not None
        assert result.waves == tuple(tuple(wave) for wave in case["expectedWaves"])
        assert result.dag.nodes[0] == base.nodes[0]


def test_domain_contract_round_trips_public_fields_and_defaults() -> None:
    payload = {
        "version": "1.0",
        "nodes": [{"id": "A", "instructions": "Run A"}],
        "defaultModel": "fake:test",
        "caps": DEFAULT_CAPS,
    }

    decoded = DagSpec.model_validate(payload)
    encoded = decoded.model_dump(mode="json", by_alias=True)

    assert encoded == {
        **payload,
        "nodes": [
            {
                **payload["nodes"][0],
                "dependsOn": [],
                "model": None,
                "tools": None,
                "outputSchema": None,
                "canExpand": False,
            }
        ],
    }
    assert DagSpec.model_validate(encoded) == decoded


@pytest.mark.parametrize(
    "path",
    [
        ("version",),
        ("nodes",),
        ("nodes", 0, "id"),
        ("nodes", 0, "instructions"),
        ("caps", "maxNodes"),
        ("caps", "maxRounds"),
        ("caps", "maxConcurrent"),
        ("caps", "tokenBudget"),
    ],
)
def test_domain_contract_rejects_each_missing_required_field(
    path: tuple[str | int, ...],
) -> None:
    payload: dict[str, Any] = {
        "version": "1.0",
        "nodes": [{"id": "A", "instructions": "Run A"}],
        "defaultModel": "fake:test",
        "caps": deepcopy(DEFAULT_CAPS),
    }
    parent: Any = payload
    for part in path[:-1]:
        parent = parent[part]
    del parent[path[-1]]

    with pytest.raises(PydanticValidationError) as raised:
        DagSpec.model_validate(payload)

    assert path in {error["loc"] for error in raised.value.errors()}


def test_contract_suite_has_no_network_dependency(
    manifest: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("contract validation attempted network access")

    monkeypatch.setattr(socket, "create_connection", fail_network)

    for case in manifest["dagCases"].values():
        validate_dag(_dag(case))
