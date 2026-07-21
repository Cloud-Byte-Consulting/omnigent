# OpenAI Codex configuration

Verified on 2026-07-20 with `codex-cli 0.144.0` against the official [Codex MCP configuration documentation](https://learn.chatgpt.com/docs/extend/mcp). The committed [`codex.toml`](codex.toml) uses the documented `[mcp_servers.<name>]` stdio fields.

## Prerequisites and configuration

Install the pinned Flow package as described in [`../PACKAGING.md`](../PACKAGING.md) so `flow-mcp` is on `PATH`. The entrypoint is independent of the working directory; no `cwd` is required. It needs permission to start a local child process. Dapr access and provider credential references are deployment concerns and are not stored in this client snippet.

Copy the `mcp_servers.flow` table from `codex.toml` into the applicable trusted project `.codex/config.toml` or user `~/.codex/config.toml`. The equivalent CLI command is:

```console
codex mcp add flow --env FLOW_LOG_LEVEL=INFO -- flow-mcp
codex mcp get flow
```

`FLOW_LOG_LEVEL=INFO` is a non-secret default. If a deployment needs credentials, forward named environment variables from local secret storage; never put credential values or approval tokens in committed TOML.

Restart Codex or begin a new task after changing configuration. Confirm that `flow` is enabled and that the available tools are exactly `propose_dag`, `run_workflow`, `get_workflow_status`, and `list_workflows`. The machine-readable, redacted record is in [`codex-evidence.json`](codex-evidence.json).

## Native conformance gate

The opt-in gate builds the Flow wheel twice with a fixed source epoch, requires identical SHA-256 hashes, installs it outside the checkout, launches a real Dapr worker, and invokes every Flow operation through `codex exec --ephemeral --json`. Each invocation ignores user configuration and repository rules, disables shell, web, apps, hooks, goals, memories, and multi-agent surfaces, exposes only the expected Flow tool, and supplies the requested arguments through stdin. Flow's preview and signed approval-token exchange remains the workflow authorization boundary.

The gate covers the shared fan-out/fan-in fixture, invalid graphs without dispatch, preview, stale approval, idempotent replay, status/list visibility, bounded expansion and cap denial, provider substitution, forced interruption/recovery, process cleanup, and evidence redaction. Missing Codex authentication, Dapr, Docker, or another explicitly required prerequisite fails the enabled gate instead of skipping it.

```console
FLOW_CODEX_E2E=1 uv run pytest -q -vv -s tests/flow/test_codex_conformance_e2e.py
```

The committed result summary is [`codex-conformance-evidence.json`](codex-conformance-evidence.json). Raw Codex JSONL, agent prose, approval tokens, signing keys, authentication values, and provider payloads are excluded from committed evidence; invocation-scoped local state is deleted after each attempt.

## Troubleshooting

- If `codex mcp get flow` cannot find the server, verify which config layer is active and that the repository is trusted before relying on project-local configuration.
- If startup fails, run `flow-mcp` from the same shell and confirm that its installation directory is on `PATH`. Increase `startup_timeout_sec` only after ruling out an invalid executable.
- MCP protocol traffic belongs on stdout. Flow diagnostics belong on stderr; inspect Codex and terminal logs without copying secrets into an issue.
- If tool discovery differs, verify the installed `omnigent` version and reinstall the pinned package. Do not rename tools in the client configuration.
- If the native gate reports no tool call, run `codex login status`. The gate uses saved Codex authentication while deliberately ignoring ambient user configuration.
