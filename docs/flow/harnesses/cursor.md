# Cursor Agent configuration

Verified on 2026-07-21 with the locally installed `agent 2026.06.04-5fd875e`. Current official sources are Cursor's [MCP configuration](https://cursor.com/docs/mcp), [CLI MCP commands](https://cursor.com/docs/cli/mcp), [permissions](https://cursor.com/docs/cli/reference/permissions), [CLI parameters](https://cursor.com/docs/cli/reference/parameters), and [Agent CLI naming/MCP changelog](https://cursor.com/changelog/cli-jan-08-2026).

## Prerequisites and configuration

Install the pinned Flow package from [`../PACKAGING.md`](../PACKAGING.md) so `flow-mcp` is on `PATH`. The packaged command has no working directory dependency, so no `cwd` is required. Cursor owns the lifecycle of a local stdio server.

Copy the exact committed [`cursor.json`](cursor.json) snippet to project-root `.cursor/mcp.json`. The user-level alternative is `~/.cursor/mcp.json`. Although simple examples sometimes omit `type`, Cursor's current stdio field table marks `type` and `command` as required, so the snippet includes `"type": "stdio"`. Repository-local `.cursor/` state is intentionally ignored; the reviewed source remains under `docs/flow/harnesses/`.

`FLOW_LOG_LEVEL=INFO` is non-sensitive. The remaining entries are `${env:NAME}` references for Flow's required runtime settings; they contain no values and make Cursor forward only the named variables to the MCP child. Export those variables through the deployment secret/runtime environment. Do not commit values or approval tokens.

Restart Cursor after changing a custom MCP server, or start a fresh `agent` process from the project. Then verify:

```console
agent mcp enable flow
agent mcp list
agent mcp list-tools flow
```

Expect project source, stdio transport, connected status, and exactly `propose_dag`, `run_workflow`, `get_workflow_status`, and `list_workflows`. Cursor asks before MCP tool calls by default. Keep that review-first behavior or allow only individual `Mcp(flow:<tool>)` entries in `.cursor/cli.json`; avoid blanket MCP approval unless it is an explicit operator decision.

## Native conformance gate

The CLO-184 gate uses Cursor's native noninteractive interface and structured event stream:

```console
CURSOR_API_KEY=<secret> FLOW_CURSOR_E2E=1 uv run pytest -q -vv -s tests/flow/test_cursor_conformance_e2e.py
```

The gate builds the Flow wheel twice, requires matching SHA-256 digests, installs the wheel into an isolated environment, and copies the committed `cursor.json` contract into a temporary workspace. Each Cursor process receives a project-local `.cursor/cli.json` that allows only the requested `Mcp(flow:<tool>)`, denies the other three Flow tools plus shell/write/web actions, and uses a fresh temporary home. It does not use `--force` or `--yolo`.

Evidence comes only from a correlated `tool_call` start/completion pair in Cursor's `stream-json` output plus independent durable Dapr state. Model prose, plain text summaries, unmatched tool events, and retry/reconnect streams are rejected. Prompts are delivered over stdin so approval tokens do not appear in the process argument list. Raw event streams and credential values are never persisted in the repository or evidence output.

The current execution state is recorded in [`cursor-conformance-evidence.json`](cursor-conformance-evidence.json). The installed CLI is present, but the saved login currently exposes no models and headless execution requires `CURSOR_API_KEY`, so the full native gate is explicitly blocked rather than reported as passing.

## Troubleshooting

- If the server is absent, confirm the file is exactly `.cursor/mcp.json`, start Agent from inside the repository, and check whether `flow` was disabled.
- If connection fails, run `command -v flow-mcp`; use an absolute executable only for a local non-portable override, never in the committed snippet.
- MCP protocol messages use stdout and Flow diagnostics use stderr. Inspect Cursor's MCP output without copying secrets into issue evidence.
- If tool listing differs, verify the installed `omnigent` version, restart Cursor, enable the server, and repeat `agent mcp list-tools flow`.
