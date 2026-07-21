import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


class _HealthyDaprHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@pytest.fixture
def flow_discovery_env(tmp_path: Path) -> Generator[dict[str, str], None, None]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthyDaprHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "FLOW_MODE": "conformance",
            "FLOW_ACTOR": "discovery-operator",
            "FLOW_SIGNING_KEY": "discovery-only-signing-key",
            "FLOW_APPROVAL_DB": str(tmp_path / "approvals.sqlite3"),
            "FLOW_APPROVAL_TTL_SECONDS": "300",
            "FLOW_DAPR_HEALTH_TIMEOUT_SECONDS": "1",
            "DAPR_GRPC_PORT": "50101",
            "DAPR_HTTP_PORT": str(server.server_port),
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
