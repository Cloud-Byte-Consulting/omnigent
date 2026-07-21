# Durable audit events

Every material Flow transition is represented by one provider-neutral
`AuditEvent`. The JSON contract contains a stable `eventId`, `runId`, optional
`nodeId`, event `type`, timezone-aware timestamp, source, correlation key, safe
summary, redacted JSON metadata, and store-assigned sequence.

Event IDs are SHA-256 hashes of the logical identity: run, node, type, and
correlation/idempotency key. Timestamps and delivery attempts do not change that
identity, so replay returns the existing event. Logical node completion uses its
`nodeExecutionId`; attempt evidence uses an attempt-specific correlation key.

```text
material transition -> normalize + redact -> stable event ID
                                               |
                              append with optimistic Dapr ETag
                                               |
                                               v
                              ordered per-run durable history
```

Metadata is recursively redacted before persistence and must be standard JSON;
Python objects and non-finite numbers are rejected. Credentials, authorization,
passwords, secrets, API keys, prompts, and raw provider payloads are replaced
with `[REDACTED]`. Defensive copies prevent later mutation of stored history,
while safe usage fields such as `tokenCount` remain available.

## Acceptance coverage

| Gherkin scenario | Automated coverage |
| --- | --- |
| Append a material transition | `test_append_material_transition_records_one_complete_ordered_event` |
| Avoid duplicate events during replay | `test_replay_with_same_logical_key_does_not_duplicate_event` |
| Redact sensitive metadata | `test_sensitive_metadata_and_summary_are_recursively_redacted` |
| Retrieve ordered history | `test_history_uses_deterministic_append_order_not_timestamps` |
| Deduplicate at-least-once node completion | `test_node_execution_id_deduplicates_completion_but_attempts_remain_traceable` |

The fake-client integration test exercises the complete Dapr state boundary and
ETag contract. The opt-in destructive Dapr E2E also proves real Redis-backed
history survives a sidecar restart, replay remains idempotent, and explicit
clean reset removes it.
