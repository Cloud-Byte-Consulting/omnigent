# Google Antigravity configuration

Verified on 2026-07-20 with Antigravity IDE 2.1.1 (embedded IDE 1.107.0). Google documents the IDE configuration workflow in [Getting Started with Google Antigravity](https://codelabs.developers.google.com/getting-started-google-antigravity), the local `command`/`args`/`env` shape in its [Antigravity CLI MCP guide](https://codelabs.developers.google.com/genai-for-dev-antigravity-cli), and the same shape plus raw-config UI in the [Cloud Storage MCP guide](https://docs.cloud.google.com/storage/docs/pre-built-tools-with-mcp-toolbox).

## Prerequisites and configuration

Install the pinned Flow package from [`../PACKAGING.md`](../PACKAGING.md) so `flow-mcp` is on the GUI application's inherited `PATH`. The command has no working directory dependency, so the snippet omits a directory. It requires permission to start a local process and project permission to use the four Flow MCP tools.

In Antigravity IDE, open the agent panel menu, choose **MCP Servers → Manage MCP Servers → View raw config**, and merge [`antigravity.json`](antigravity.json) into `~/.gemini/config/mcp_config.json`. Do not replace unrelated `mcpServers` entries. Antigravity CLI uses different global and workspace paths; this child issue targets the IDE.

`FLOW_LOG_LEVEL=INFO` is non-sensitive. Provider credentials and approval tokens must not be stored in this JSON. Use deployment-managed credential references where a provider requires them.

Save the file, then open **Settings → Customizations → Installed MCP Servers** and click **Refresh**. Enable `flow`, open its details, and verify exactly `propose_dag`, `run_workflow`, `get_workflow_status`, and `list_workflows`. Approve access to those tools for the current project when prompted. The redacted machine-readable result is [`antigravity-evidence.json`](antigravity-evidence.json).

## Troubleshooting

- If `flow` is absent after Refresh, confirm that the IDE opened `~/.gemini/config/mcp_config.json`, the JSON parses, and the entry is nested under `mcpServers`.
- If launch fails, run `flow-mcp` in a terminal launched from the same desktop environment and confirm its installation directory is inherited on `PATH`.
- MCP protocol messages use stdout. Flow diagnostics use stderr; inspect the IDE MCP logs without pasting credentials into an issue.
- If the server is present but tools are hidden, check project-specific MCP tool permissions and the server toggle, then Refresh again.
