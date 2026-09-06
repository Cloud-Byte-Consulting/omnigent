# Flow MCP harness configurations

These reviewed configurations expose one Python 3.12+ Flow package to GitHub Copilot, Google Antigravity, OpenAI Codex, Cursor Agent, and OpenCode. [`index.json`](index.json) is the machine-readable traceability map.

## Common contract

Every harness starts `flow-mcp` as a local stdio child process. No working directory is required. Raw discovery must return exactly `propose_dag`, `run_workflow`, `get_workflow_status`, and `list_workflows`. Protocol traffic uses stdout and redacted diagnostics use stderr.

`FLOW_LOG_LEVEL=INFO` is the only common client environment setting. It is non-sensitive. No credential values or approval tokens belong in any committed configuration. Provider credential references, Dapr endpoints, and persistence settings belong to deployment configuration.

Install the pinned artifact first using [`../PACKAGING.md`](../PACKAGING.md). The `flow-mcp` executable must be on the harness process's `PATH`. After applying a config, use the harness-specific restart/reload and verification steps; do not infer one client's behavior from another.

## Harness-specific differences

| Harness | Reviewed config | Guide | Evidence | Key difference |
| --- | --- | --- | --- | --- |
| GitHub Copilot CLI | [`.mcp.json`](../../../.mcp.json) | [`copilot.md`](copilot.md) | [`copilot-evidence.json`](copilot-evidence.json) | Repo-root workspace config, trusted-folder gate, raw tool allowlist, `${VAR:-default}` expansion |
| Google Antigravity IDE | [`antigravity.json`](antigravity.json) | [`antigravity.md`](antigravity.md) | [`antigravity-evidence.json`](antigravity-evidence.json) | Merge into the IDE raw config and Refresh Installed MCP Servers |
| OpenAI Codex | [`codex.toml`](codex.toml) | [`codex.md`](codex.md) | [`codex-evidence.json`](codex-evidence.json) | TOML `mcp_servers.flow`, trusted project config or user config, explicit startup/tool timeouts |
| Cursor Agent | [`cursor.json`](cursor.json) | [`cursor.md`](cursor.md) | [`cursor-evidence.json`](cursor-evidence.json) | Copy to `.cursor/mcp.json`, explicit stdio type, enable and list tools with `agent mcp` |
| OpenCode | [`opencode.json`](opencode.json) | [`opencode.md`](opencode.md) | [`opencode-evidence.json`](opencode-evidence.json) | Local command array, explicit timeout, and approval for namespaced `flow_run_workflow` |

All five rows are verified as of the date and version in their evidence file. If a future interface cannot be reverified, change its `index.json` status to `unverified`, record the missing evidence in that harness guide, and state the next official verification action; never carry a stale passing claim forward.

Native full-workflow evidence is separate from static configuration evidence. Cursor's CLO-184 record is [`cursor-conformance-evidence.json`](cursor-conformance-evidence.json); a blocked native gate remains visibly blocked until its structured event and durable-state checks pass.

## Dependency and test ownership

- Parent roll-up: CLO-146.
- Configuration children: CLO-176 through CLO-180.
- Shared immutable E2E fixtures: CLO-174.
- Native full-workflow children: CLO-181 through CLO-185 under CLO-164.
- Static and integration checks: `tests/flow/test_*_configuration.py` plus `tests/flow/test_harness_rollup.py`.
