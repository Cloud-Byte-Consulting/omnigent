from datetime import UTC, datetime, timedelta

from omnigent.flow.audit import InMemoryAuditStore
from omnigent.flow.runtime_audit import (
    RUNTIME_AUDIT_ACTIVITY_NAME,
    RuntimeAuditActivity,
    register_runtime_audit_activity,
)

NOW = datetime(2026, 7, 21, tzinfo=UTC)


class RecordingRuntime:
    def __init__(self) -> None:
        self.name: str | None = None
        self.handler = None

    def register_activity(self, handler, *, name=None):
        self.name = name
        self.handler = handler


def test_runtime_audit_activity_persists_ordered_redacted_events_once() -> None:
    store = InMemoryAuditStore()
    ticks = iter((NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)))
    activity = RuntimeAuditActivity(store, clock=lambda: next(ticks))
    request = {
        "runId": "run-1",
        "events": [
            {
                "type": "node_running",
                "nodeId": "A",
                "source": "workflow",
                "correlationKey": "stable-A:attempt:1:running",
                "summary": "Node A is running",
                "metadata": {"attempt": 1, "credential": "sk-super-secret"},
            },
            {
                "type": "node_succeeded",
                "nodeId": "A",
                "source": "workflow",
                "correlationKey": "stable-A:succeeded",
                "summary": "Node A succeeded",
                "metadata": {"attempt": 1},
            },
        ],
    }

    first = activity.execute(request)
    replay = activity.execute(request)

    assert first == replay
    assert first == {"eventIds": [item.event_id for item in store.history("run-1")]}
    assert [item.type for item in store.history("run-1")] == [
        "node_running",
        "node_succeeded",
    ]
    assert [item.sequence for item in store.history("run-1")] == [1, 2]
    assert store.history("run-1")[0].metadata == {
        "attempt": 1,
        "credential": "[REDACTED]",
    }


def test_runtime_audit_activity_registers_the_versioned_dapr_name() -> None:
    runtime = RecordingRuntime()
    store = InMemoryAuditStore()

    handler = register_runtime_audit_activity(
        runtime,
        RuntimeAuditActivity(store, clock=lambda: NOW),
    )

    assert runtime.name == RUNTIME_AUDIT_ACTIVITY_NAME == "PersistFlowAuditEvents"
    assert runtime.handler is handler
    assert handler(
        None,
        {
            "runId": "run-1",
            "events": [
                {
                    "type": "run_running",
                    "nodeId": None,
                    "source": "workflow",
                    "correlationKey": "run-1:running",
                    "summary": "Run is running",
                    "metadata": {},
                }
            ],
        },
    )["eventIds"]
