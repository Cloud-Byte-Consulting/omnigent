# OpenCode configuration

Verified on 2026-07-20 with locally installed OpenCode 1.2.10. The latest official release reviewed was 1.18.4, released 2026-07-20. Current primary sources are the official [MCP](https://opencode.ai/docs/mcp-servers/), [configuration](https://opencode.ai/docs/config/), [CLI](https://opencode.ai/docs/cli/), [tool permission](https://opencode.ai/docs/tools/), and [live schema](https://opencode.ai/config.json) references plus the [1.18.4 release](https://github.com/anomalyco/opencode/releases/tag/v1.18.4).

## Prerequisites and configuration

Install the pinned Flow package from [`../PACKAGING.md`](../PACKAGING.md) so `flow-mcp` is on `PATH`. Copy the exact committed [`opencode.json`](opencode.json) fields into project-root `opencode.json`; global configuration lives at `~/.config/opencode/opencode.json`, and `OPENCODE_CONFIG` may name a custom file. The packaged entrypoint has no working directory dependency, so `cwd` is omitted.

OpenCode local MCP uses `type: local`, a command array, optional environment, and timeout in milliseconds. The explicit 30-second timeout avoids a current mismatch between the documented/schema default and the tagged runtime default.

`FLOW_LOG_LEVEL=INFO` is non-sensitive. For a future secret, use `{env:NAME}` or `{file:path}` substitution and keep the value outside git. Missing environment substitution becomes an empty string, so deployments must fail closed when a credential is required.

The `flow_run_workflow: ask` permission keeps the state-changing tool behind an OpenCode prompt. Other Flow tools retain the default policy. Use `flow_*: ask` if the operator requires approval for every Flow tool.

After changing configuration, exit and relaunch OpenCode; current official docs do not promise hot reload, so a fresh process is the conservative verification path. Run:

```console
opencode mcp list
opencode .
```

Expect `flow` to report connected. In a session, ask OpenCode to use `flow_list_workflows`; model-facing tools are prefixed with the MCP server name, while raw MCP discovery remains exactly `propose_dag`, `run_workflow`, `get_workflow_status`, and `list_workflows`.

## Native conformance gate

The opt-in native gate uses the installed OpenCode binary, an isolated configuration directory, the public `opencode/deepseek-v4-flash-free` model, a twice-built pinned Flow wheel, and local Docker/Dapr services. Each invocation owns an empty disposable Redis container on a random loopback port for the full gate lifetime and force-removes it on exit. Concurrent or historical state therefore cannot be reused, and the gate does not consume finite shared Redis database slots. Run it from the repository root:

```console
FLOW_OPENCODE_E2E=1 uv run pytest -q -vv -s tests/flow/test_opencode_conformance_e2e.py
```

Passing evidence requires exact tool discovery plus all six shared scenario groups: the approved three-node DAG, invalid graphs, status and list, bounded expansion, interruption recovery, and provider substitution. The evidence must bind the result to the Flow commit, two identical wheel hashes built with the shared `SOURCE_DATE_EPOCH=1767225600`, the OpenCode binary hash, the sorted OpenCode model-catalog hash, and six globally unique durable run IDs. A pass also records the canonical succeeded fan-in and idempotent replay result, plus explicit proof that invalid graphs and stale approvals did not dispatch, caps were enforced, provider output was normalized, and recovery effects ran exactly once. Raw native events, model prose, credentials, signing values, and approval tokens are never committed; the redaction flags must remain exactly false, false, and required.

The committed result is [`opencode-conformance-evidence.json`](opencode-conformance-evidence.json). Its current state is deliberately `blocked`. Exact native discovery passed, and on a freshly selected empty non-default Redis database the model produced the expected typed `confirm:false` preview result with `approval_required`. The native operation then made an additional `flow_run_workflow` call and created an unexpected durable run, so preview-without-dispatch failed. Raw events, the durable run ID, and all secret-bearing values are intentionally omitted. The scenario assertion, canonical result, safety result, artifact, and run-ID fields therefore remain `null`; no passing payload is inferred. The model catalog digest is calculated from `opencode models opencode | LC_ALL=C sort`; rerun that command and review any catalog change before replacing the evidence hash.

## Troubleshooting

- If `flow` is absent, check config precedence and confirm `opencode.json` is in the project root or selected through `OPENCODE_CONFIG`.
- If connection fails, run `command -v flow-mcp`; a GUI or service may inherit a different `PATH`.
- MCP protocol messages use stdout and Flow diagnostics use stderr. Use `opencode --print-logs --log-level DEBUG mcp list` without copying secrets into evidence.
- If a tool name is unexpected, distinguish OpenCode's `flow_` namespace from the four raw MCP names before changing the server contract.
- If the public model is absent, run `opencode models opencode` and stop rather than silently selecting a different model; changing the model invalidates the recorded catalog hash.
