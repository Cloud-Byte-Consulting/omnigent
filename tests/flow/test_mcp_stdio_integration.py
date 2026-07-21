import json
import signal
import subprocess
import sys
import threading
from queue import Queue
from typing import TextIO


def _send(process: subprocess.Popen[str], message: dict[str, object]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def _read_json_line(stream: TextIO) -> dict[str, object]:
    lines: Queue[str] = Queue()
    threading.Thread(target=lambda: lines.put(stream.readline()), daemon=True).start()
    line = lines.get(timeout=10)
    assert line
    value = json.loads(line)
    assert isinstance(value, dict)
    return value


def test_real_stdio_protocol_discovery_validation_and_shutdown() -> None:
    process = subprocess.Popen(
        (sys.executable, "-m", "omnigent.flow.mcp_server"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    try:
        _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "flow-test", "version": "1"},
                },
            },
        )
        initialized = _read_json_line(process.stdout)
        assert initialized["id"] == 1
        _send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        _send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        discovered = _read_json_line(process.stdout)
        tools = discovered["result"]["tools"]
        assert [tool["name"] for tool in tools] == [
            "propose_dag",
            "run_workflow",
            "get_workflow_status",
            "list_workflows",
        ]

        _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "list_workflows", "arguments": {}},
            },
        )
        successful = _read_json_line(process.stdout)
        assert successful["result"]["isError"] is False

        _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "propose_dag", "arguments": {}},
            },
        )
        invalid = _read_json_line(process.stdout)
        assert invalid["result"]["isError"] is True
        assert "invalid_input" in invalid["result"]["content"][0]["text"]

        process.send_signal(signal.SIGTERM)
        remaining_stdout, _stderr = process.communicate(timeout=10)
        assert process.returncode == 0
        for line in remaining_stdout.splitlines():
            json.loads(line)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_invalid_startup_configuration_stays_off_stdout() -> None:
    process = subprocess.run(
        (sys.executable, "-m", "omnigent.flow.mcp_server"),
        env={"FLOW_LOG_LEVEL": "not-a-level"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert process.returncode == 2
    assert process.stdout == ""
    assert "FLOW_LOG_LEVEL" in process.stderr
