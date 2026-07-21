# GitHub Copilot CLI configuration

Verified on 2026-07-21 with GitHub Copilot CLI 1.0.73. Sources are GitHub's official [MCP setup](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers), [command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference), [tool permissions](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/allowing-tools), [configuration locations](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference), and [CLI changelog](https://github.com/github/copilot-cli/blob/main/changelog.md).

## Prerequisites and configuration

Install the pinned Flow package from [`../PACKAGING.md`](../PACKAGING.md) so `flow-mcp` is on `PATH`. The exact workspace configuration is committed at repository-root [`.mcp.json`](../../../.mcp.json). Copilot CLI also accepts `.github/mcp.json`; user configuration is `~/.copilot/mcp-config.json`. Workspace configuration requires folder trust.

The packaged entrypoint has no working directory dependency, so `cwd` is omitted. Copilot inherits `PATH`; it expands `${FLOW_LOG_LEVEL:-INFO}` to the local value or the non-sensitive `INFO` default. Before starting, export `FLOW_MODE=conformance`, `FLOW_ACTOR`, `FLOW_SIGNING_KEY`, `FLOW_APPROVAL_DB`, `FLOW_APPROVAL_TTL_SECONDS`, `DAPR_GRPC_PORT`, and `DAPR_HTTP_PORT`. Keep those runtime values outside committed configuration; never add provider credentials, signing keys, or approval tokens to the file.

Start a new `copilot` session from the trusted repository after editing the workspace file. GitHub documents immediate availability for servers added interactively, but does not promise hot reload for hand-edited workspace JSON. Verify deterministically with:

```console
copilot mcp list --json
copilot mcp get flow --json
```

The raw server contract must list exactly `propose_dag`, `run_workflow`, `get_workflow_status`, and `list_workflows`. Copilot may expose model-facing names with a `flow-` prefix; that namespace does not change the raw MCP names.

Copilot requests permission for MCP tool calls. Review and persist only the intended repo-scoped approvals; do not use blanket allow-all flags. An enterprise MCP allowlist can independently block the server.

## Native conformance gate

The opt-in gate builds the wheel twice with a fixed source epoch, requires identical hashes, installs it outside the checkout, launches an allowlisted worker environment with real Dapr, and invokes each Flow operation through the real Copilot CLI. It checks the exact shared A/B-to-C fixture and durable output, invalid graphs without dispatch, preview and stale approval, idempotent replay, status/list visibility, accepted and cap-denied expansion, provider substitution, forced interruption/recovery, process cleanup, and evidence redaction. Missing Copilot authentication, Dapr, Docker, or other prerequisites fail the explicitly enabled gate instead of skipping it.

```console
FLOW_COPILOT_E2E=1 uv run pytest -q -vv -s tests/flow/test_copilot_conformance_e2e.py
```

The committed result summary is [`copilot-conformance-evidence.json`](copilot-conformance-evidence.json). Raw Copilot JSONL, approval tokens, signing keys, and authentication values remain memory-only in ephemeral test directories.

## Troubleshooting

- If `flow` is absent, verify `.mcp.json` is at the repository root, the folder is trusted, and no higher-priority user/plugin configuration masks it.
- If launch fails, run `command -v flow-mcp`. A package installed only inside an inactive virtual environment is not on Copilot's inherited `PATH`.
- Use `copilot mcp get flow --json` or interactive `/mcp show flow` to inspect status and tools. Client logs are under `~/.copilot/logs`.
- MCP JSON-RPC uses stdout; Flow diagnostics use stderr. Copilot startup warnings include stderr, which must remain redacted in evidence.
