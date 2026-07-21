# Flow MCP packaging and operation

Flow ships inside the Apache-2.0-licensed `omnigent` Python distribution. The approved runtime is Python 3.12 or newer, dependencies are resolved from `uv.lock`, and the executable stdio entrypoint is `flow-mcp`.

## Install and launch

For a released version, install into an isolated tool environment:

```console
uv tool install omnigent==<version>
flow-mcp
```

For a one-off pinned launch, use `uvx --from omnigent==<version> flow-mcp`. MCP clients should launch that command over stdio. A successful Inspector connection discovers exactly `propose_dag`, `run_workflow`, `get_workflow_status`, and `list_workflows`.

The server writes only MCP traffic to stdout. Diagnostics go to stderr. `FLOW_LOG_LEVEL` accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`; it contains no secret. Dapr endpoints, state-store components, signing keys, actor identity, and provider credential references belong to the deployment configuration. Never put credential values in an MCP client file or package artifact.

## Upgrade and uninstall

Upgrade predictably with `uv tool upgrade omnigent`, or pin the desired version with `uv tool install --force omnigent==<version>`. Review release compatibility notes before changing a major version. Flow's SQLite approval store adds new columns automatically and treats legacy approval rows as invalid until a new approval is recorded; public JSON contract versions remain explicit in every DAG.

Uninstall with `uv tool uninstall omnigent`. State stored by Dapr or SQLite is operator data and is intentionally not deleted by package uninstall.

## Reproducible build and integrity

Build from a clean checkout at the selected source revision:

```console
uv sync --frozen
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
uv build --wheel --out-dir dist
shasum -a 256 dist/omnigent-*.whl > dist/SHA256SUMS
```

The reproducibility criterion is an identical SHA-256 digest when source revision, Python version, `uv.lock`, build backend, platform, and `SOURCE_DATE_EPOCH` are unchanged. Publish the wheel and `SHA256SUMS` together. `docs/flow/dependency-inventory.json` is the Flow-specific dependency inventory; `uv.lock` is the complete resolved inventory with upstream artifact hashes.

## Clean-environment verification

Install the wheel into a fresh Python 3.12+ environment, then run `flow-mcp` through MCP Inspector or another standards-compatible MCP client. The automated packaging integration test builds the wheel, installs its contents outside the checkout, launches the packaged module over stdio, and verifies all four tool schemas. It also scans the archive for private-key and live-token markers.

Remote/container deployment may wrap the same application service, but must not change the four JSON tool contracts, approval semantics, or error codes.
