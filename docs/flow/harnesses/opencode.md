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

## Troubleshooting

- If `flow` is absent, check config precedence and confirm `opencode.json` is in the project root or selected through `OPENCODE_CONFIG`.
- If connection fails, run `command -v flow-mcp`; a GUI or service may inherit a different `PATH`.
- MCP protocol messages use stdout and Flow diagnostics use stderr. Use `opencode --print-logs --log-level DEBUG mcp list` without copying secrets into evidence.
- If a tool name is unexpected, distinguish OpenCode's `flow_` namespace from the four raw MCP names before changing the server contract.
