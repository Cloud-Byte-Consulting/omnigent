# Provider failure normalization

Each adapter registration owns an immutable mapping from provider error codes to
Flow's stable categories: `configuration`, `authentication`, `rate_limit`,
`transient`, `invalid_output`, `budget`, and `permanent`. A mapping also supplies
the safe public message and whether policy may retry the failure. Unknown codes
become non-retryable permanent failures.

Raw exception text and provider payloads never cross the router. The normalized
failure contains only the safe mapping message, selected provider/model,
attempt, validated provider request ID, retry-after guidance, already-consumed
usage, and latency. Credential-shaped request IDs are dropped.

```text
adapter code -> configured safe category -> normalized failure
                                                |
                 run retry policy + cancellation/deadline/budget
                                                |
                                retry(delay, next attempt) | terminal
```

Retry delay is the larger of bounded exponential backoff and provider
retry-after guidance. No retry is scheduled at or beyond the run's attempt,
elapsed-time, deadline, cancellation, or token-budget boundary. An
`invalid_output` mapping may be retryable or terminal according to the selected
run/adapter policy.

## Acceptance coverage

| Gherkin scenario | Automated coverage |
| --- | --- |
| Classify a provider failure | `test_adapter_mapping_classifies_provider_failure` |
| Respect provider retry guidance | `test_retry_delay_respects_provider_guidance_and_records_next_attempt` |
| Stop retrying at policy limit | `test_policy_limits_return_terminal_failure` |
| Sanitize diagnostics | `test_normalized_failure_preserves_safe_guidance_request_id_and_usage` |

All tests are deterministic and offline. Provider-boundary integration and
five-harness end-to-end coverage are inherited by their downstream conformance
issues.
