# Structured output enforcement

When a node declares `outputSchema`, the provider router first requires an
adapter with structured-output capability. Every returned payload is then
validated locally with Draft 2020-12, even if the provider claims strict schema
enforcement. A `NodeExecutionSuccess` is returned only after this local check,
so dependents never receive invalid data.

Validation failures contain deterministic JSON-pointer paths, validator names,
and value-free messages that cannot echo sensitive provider output. The invalid
payload itself is discarded.

```text
provider output -> local Draft 2020-12 validation -> success for dependents
                         |
                  stable safe violations
                         |
             retry + token budget allow repair?
                    | yes              | no
             same schema/errors      terminal invalid_output
```

A repair keeps the original schema and passes safe validation errors to the
adapter on the next numbered attempt. Each provider call is persisted through
the usage service before another budget-dependent dispatch. Retry policy,
remaining tokens, cancellation, and deadline all gate the next attempt. Nodes
without `outputSchema` return their normalized unconstrained payload.

## Acceptance coverage

| Gherkin scenario | Automated coverage |
| --- | --- |
| Accept conforming output | `test_accepts_locally_conforming_output_for_dependents` |
| Reject non-conforming output | `test_rejects_nonconforming_output_with_stable_json_paths` |
| Repair within policy | `test_repair_reuses_schema_and_errors_and_records_both_attempts` |
| Reject an incapable adapter | `test_incapable_adapter_is_rejected_without_provider_call` |
| Validate provider-strict output locally | `test_provider_strict_claim_never_skips_local_validation` |

The focused suite also proves unconstrained output behavior, JSON-pointer
escaping, and that repair diagnostics do not echo invalid values. Provider
conformance integration and five-harness end-to-end coverage remain downstream.
