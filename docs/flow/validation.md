# Flow validation contract

`omnigent.flow.validate_dag` decodes the language-neutral `DagSpec` and performs
pure pre-dispatch validation. It returns either a normalized DAG with stable
topological waves or every independently detectable error as:

```json
{"code": "dangling_dependency", "path": "/nodes/0/dependsOn/0", "message": "..."}
```

Errors have deterministic code/path/message ordering. Validation performs no
provider, Dapr, MCP, network, or filesystem writes. Per-node `outputSchema`
values are checked as JSON Schema Draft 2020-12.

`execution_waves` preserves input order inside each topological level.
`dispatch_batches` splits a wave by `maxConcurrent` without changing dependency
membership. `validate_expansion` accepts all proposed nodes or none; it checks
the proposer, next round, combined graph, node/round caps, and remaining token
budget without mutating the existing DAG.

The reusable language-neutral fixture matrix lives at
`tests/flow/fixtures/contracts/manifest.json`.

## Gherkin-to-test coverage

| Linear scenario | Automated evidence |
| --- | --- |
| CLO-137: Accept a valid workflow | `test_valid_fixtures_produce_deterministic_waves` |
| CLO-137: Reject an invalid workflow before dispatch | `test_invalid_fixtures_return_stable_actionable_errors` |
| CLO-137: Compute concurrent execution waves | `test_valid_fixtures_produce_deterministic_waves`, `test_dispatch_batches_limit_concurrency_without_changing_waves` |
| CLO-137: Reject an unsafe expansion | `test_expansion_fixtures_are_atomic` |
| CLO-137: Remain language and provider neutral | `test_published_schema_is_language_neutral` |
| CLO-140: Run offline | `test_contract_suite_has_no_network_dependency` |
| CLO-140: Cover every required defect | manifest-driven DAG and expansion parameterizations |
| CLO-140: Produce deterministic results | repeated-result assertions in the DAG fixture tests |
| CLO-140: Fail on contract drift | `test_checked_in_schema_matches_python_contract` and manifest expected results |
