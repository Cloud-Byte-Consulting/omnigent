# Inspect local Flow workflows with Dapr 1.18

Use the Dapr Workflow CLI as the supported native inspection view for local
Flow runs. Dapr CLI 1.18 removed the legacy `dapr dashboard` command, so that
command is not a prerequisite for operating or debugging Flow. The optional
Diagrid Dev Dashboard is a graphical alternative, but Flow's machine-readable
status contract remains the source of truth.

## Start and verify the local runtime

In Terminal 1, start the worker and leave the command running:

```console
python -m omnigent.flow.local_dapr start
```

In Terminal 2, verify readiness:

```console
python -m omnigent.flow.local_dapr status
dapr --version
dapr list --output json
curl -fsS http://127.0.0.1:3510/v1.0/metadata | jq
```

The safe local status command reports component health for Docker, the Dapr
infrastructure, the Flow sidecar, and the workflow worker without printing
credentials or component metadata values. In the Metadata API response, verify
that the app ID is `omnigent-flow`, runtime version is `1.18.1`, the
`flowstatestore` component is loaded, and `workflows.connectedWorkers` is at
least one.

## Inspect a run

```console
python -m omnigent.flow.local_dapr inspect-list
python -m omnigent.flow.local_dapr inspect-history <run-id>
```

These repository wrappers call the Dapr Workflow CLI and return an allowlisted
projection. The list view exposes the Dapr runtime state, workflow name and
instance ID, created and last-update timing, plus Flow run/node states, attempts,
and failure category. History shows ordered workflow and activity history event
names, their timing and elapsed duration, status, and execution IDs.

Raw Dapr JSON may contain workflow/activity inputs, outputs, event details,
custom status, and failure messages. Do not attach raw `dapr workflow ...
--output json` output to an issue or review. The safe wrappers omit those
payload-bearing fields. Retry timers and active activity reminders can be
inspected locally with:

```console
dapr scheduler list --filter workflow --output wide
dapr scheduler list --filter activity --output wide
```

Dapr evidence is intentionally paired with Flow's normalized view. Call the
`get_workflow_status` MCP tool with the same run ID to inspect:

- approved DAG identity and the normalized run state;
- node dependencies, blocked state, timing, and explicit attempts;
- provider/model selection and token usage;
- approval decision and audit history;
- configured caps, current utilization, and dynamic expansions;
- normalized failure category, retryability, and policy exhaustion.

Native Dapr history is transport/runtime evidence. It does not normalize
approval, attempts, caps, usage, expansions, provider identity, or Flow's
redaction and authorization rules. `customStatus` is opaque to Dapr, and the
CLI can truncate event details. Do not infer missing Flow fields from it.

One Dapr 1.18 edge case matters during incident response: `STALLED` may appear
in workflow output, but the CLI does not accept it as a list filter. List all
runs as JSON and filter the result externally, then inspect that run's history.

## Graphical inspection

`dapr dashboard` is expected to fail on CLI 1.18 because the command was
removed. Dapr's current debugging guide points to the optional Diagrid Dev
Dashboard for graphical inspection. Installing that third-party UI is not
required for Flow and is outside the reproducible repository setup. Treat UI
screenshots and component-detail pages as potentially sensitive; never expose
inline secrets or provider payloads.

## Reproduce the recovery evidence

The canonical end-to-end tests cover both recovery and diagnosis. The first
starts a valid three-node Flow DAG, stops the Dapr app after the first parallel
wave, restarts it, and proves completion without duplicate node side effects.
The second deliberately produces invalid structured output twice and proves the
safe Dapr view and `get_workflow_status` both show the failed node and attempt
count:

```console
FLOW_DAPR_E2E=1 uv run pytest -q tests/flow/test_flow_runtime_e2e.py::test_three_node_dag_recovers_mid_wave_without_duplicate_effects
FLOW_DAPR_E2E=1 uv run pytest -q tests/flow/test_flow_runtime_e2e.py::test_failed_activity_is_visible_in_safe_dapr_and_flow_views
```

Each test stops the sidecar in cleanup and uses a unique run ID. After either
test completes, restart the worker in Terminal 1. Then, in Terminal 2, locate
the newest failed diagnostic run and inspect it without exposing raw payloads:

```console
python -m omnigent.flow.local_dapr start
RUN_ID=$(python -m omnigent.flow.local_dapr inspect-list | jq -r '[.[] | select(.instanceID | startswith("flow-failed-inspection-"))] | sort_by(.lastUpdate) | last | .instanceID')
python -m omnigent.flow.local_dapr inspect-history "$RUN_ID"
```

The first command above belongs in Terminal 1 and blocks while the worker is
running; run the other two in Terminal 2. Stop Terminal 1 with Ctrl-C when
inspection is complete. A finished run can legitimately have no active
Scheduler entries once all retry timers and activities have completed.

## Official references

- [Dapr workflow CLI reference](https://docs.dapr.io/reference/cli/dapr-workflow/)
- [Manage workflows](https://docs.dapr.io/developing-applications/building-blocks/workflow/howto-manage-workflow/)
- [Metadata API](https://docs.dapr.io/reference/api/metadata_api/)
- [Workflow features and retry behavior](https://docs.dapr.io/developing-applications/building-blocks/workflow/workflow-features-concepts/)
- [Dapr CLI 1.18.0 release](https://github.com/dapr/cli/releases/tag/v1.18.0)
- [Removal of the deprecated dashboard](https://github.com/dapr/cli/pull/1647)
