# Flow contracts

Flow publishes Draft 2020-12 JSON Schemas for `DagSpec` and
`ExpansionRequest` in `omnigent/flow/schemas`. Their stable `$id` values and
JSON field names are the public contract; consumers do not import Python.

Regenerate the checked-in schemas after changing the Python contract models:

```console
uv run python scripts/export_flow_schemas.py
uv run --extra dev pytest tests/flow/test_schema_contract.py
```

The structural schema checks required fields, types, non-empty collections,
positive caps, and `provider:model` references. Graph-wide rules—unique node
IDs, dependency references, cycles, cap totals, proposer state, remaining token
budget, and atomic expansion—are semantic checks performed before execution.

Provider structured-output implementations may support only a subset of Draft
2020-12. Adapters may reject unsupported keywords with a clear error, but must
not alter or fork these schemas. The full public schema remains the validation
source of truth.
