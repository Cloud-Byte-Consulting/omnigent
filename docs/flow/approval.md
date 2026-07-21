# Revision-bound approval

Flow approval happens before workflow dispatch. `ApprovalService.preview` first
validates the DAG, then returns the complete authorized review view: canonical
digest and contract version, instructions, dependencies, selected models, tool
allowlists, output schemas, caps, validation warnings, and an explicit unknown
usage estimate.

An approval or denial record contains the reviewer identity, decision time,
expiry, DAG digest, cap snapshot, model/tool snapshot, and only a SHA-256 hash of
the signed token. The token is authenticated with HMAC-SHA-256. Confirmation
recomputes every snapshot and rejects expired, forged, changed, denied, or
canceled approvals with `approval_invalid` before run creation.

```text
validated DAG -> preview (no dispatch) -> signed decision
                                            |
                     exact digest/snapshots + unexpired approval
                                            |
                                            v
                              atomic idempotent run start
```

The store owns the atomic start-once boundary. Identical confirmation retries
return the recorded run ID, including after a process restarts when using the
SQLite store. Approval is a pre-dispatch operation; it has no workflow resume or
handoff mode.

## Acceptance coverage

| Gherkin scenario | Automated coverage |
| --- | --- |
| Preview without dispatch | `test_preview_is_complete_deterministic_and_never_dispatches` |
| Start an unchanged approved revision | `test_unchanged_approval_starts_once_and_retry_returns_existing_run` |
| Reject invalid approval | `test_invalid_approval_never_starts_run` |
| Avoid duplicate runs | unit start-once test and SQLite restart integration test |
| Approve without workflow handoff | approval record contract assertion and absence of any resume API |

Unit tests own validation, digest binding, forgery/expiry/decision checks, safe
records, and retry behavior. The SQLite integration test crosses the persistence
and process-restart boundary. Five-harness end-to-end approval coverage remains
in the shared downstream conformance issue.
