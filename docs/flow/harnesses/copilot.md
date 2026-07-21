# GitHub Copilot CLI configuration

Verified on 2026-07-20 with locally installed GitHub Copilot CLI 1.0.56. The latest official changelog version reviewed was 1.0.73, released 2026-07-20. Sources are GitHub's official [MCP setup](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers), [command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference), [tool permissions](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/allowing-tools), [configuration locations](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference), and [CLI changelog](https://github.com/github/copilot-cli/blob/main/changelog.md).

## Prerequisites and configuration

Install the pinned Flow package from [`../PACKAGING.md`](../PACKAGING.md) so `flow-mcp` is on `PATH`. The exact workspace configuration is committed at repository-root [`.mcp.json`](../../../.mcp.json). Copilot CLI also accepts `.github/mcp.json`; user configuration is `~/.copilot/mcp-config.json`. Workspace configuration requires folder trust.

The packaged entrypoint has no working directory dependency, so `cwd` is omitted. Copilot inherits `PATH`; it expands `${FLOW_LOG_LEVEL:-INFO}` to the local value or the non-sensitive `INFO` default. Never add provider credentials or approval tokens to the file.

Start a new `copilot` session from the trusted repository after editing the workspace file. GitHub documents immediate availability for servers added interactively, but does not promise hot reload for hand-edited workspace JSON. Verify deterministically with:

```console
copilot mcp list --json
copilot mcp get flow --json
```

The raw server contract must list exactly `propose_dag`, `run_workflow`, `get_workflow_status`, and `list_workflows`. Copilot may expose model-facing names with a `flow-` prefix; that namespace does not change the raw MCP names.

Copilot requests permission for MCP tool calls. Review and persist only the intended repo-scoped approvals; do not use blanket allow-all flags. An enterprise MCP allowlist can independently block the server.

## Troubleshooting

- If `flow` is absent, verify `.mcp.json` is at the repository root, the folder is trusted, and no higher-priority user/plugin configuration masks it.
- If launch fails, run `command -v flow-mcp`. A package installed only inside an inactive virtual environment is not on Copilot's inherited `PATH`.
- Use `copilot mcp get flow --json` or interactive `/mcp show flow` to inspect status and tools. Client logs are under `~/.copilot/logs`.
- MCP JSON-RPC uses stdout; Flow diagnostics use stderr. Copilot startup warnings include stderr, which must remain redacted in evidence.
