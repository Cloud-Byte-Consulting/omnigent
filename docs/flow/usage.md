# Durable token-budget accounting

Flow records one normalized `UsageRecord` for every provider attempt that
reports or may have consumed tokens, including failed and retried calls. The
record retains input, output, total, provider-specific integer token classes,
provider/model, node, attempt, outcome, warnings, and a stable idempotency key.

The durable run aggregate sums normalized totals, never erases completed usage,
and computes `remainingTokens = max(tokenBudget - usedTokens, 0)`. Replaying the
same attempt record returns the existing state without double counting. A run is
`capReached` when usage meets or exceeds its immutable limit.

```text
provider attempt -> normalize or conservative estimate -> durable Dapr append
                                                           |
                                              used / remaining / limit
                                                           |
                                      pre-dispatch allow | budget failure
```

If total usage is absent but input/output counts exist, Flow sums the reported
counts. If no usable count exists, the configured conservative per-attempt
charge is applied and a visible warning is stored. Pre-dispatch checks require a
positive worst-case token amount and return current, remaining, and limit values
without invoking a provider when the call cannot fit.

## Acceptance coverage

| Gherkin scenario | Automated coverage |
| --- | --- |
| Aggregate successful node usage | `test_aggregates_successful_node_usage_and_remaining_budget` |
| Count a charged failed attempt | `test_charged_failed_attempt_counts_exactly_once_during_replay` |
| Prevent dispatch above remaining budget | `test_prevents_dispatch_when_request_cannot_fit` |
| Avoid replay double counting | unit replay test and Dapr-boundary integration test |
| Handle missing provider usage | `test_missing_usage_applies_conservative_policy_and_warning` |

Unit tests cover normalization, cap decisions, overage, warnings, idempotency,
and defensive copies. The integration test crosses the Dapr ETag persistence
boundary and verifies the record is durable before the next dispatch decision.
Five-harness end-to-end coverage remains in the downstream conformance suite.
