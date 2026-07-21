"""Minimal workflow worker used to verify the local Dapr boundary."""

from __future__ import annotations

import signal
import threading
from collections.abc import Generator

import dapr.ext.workflow as wf

runtime = wf.WorkflowRuntime()


@runtime.workflow(name="FlowRuntimeSmoke")
def smoke_workflow(
    ctx: wf.DaprWorkflowContext,
    workflow_input: object,
) -> Generator[object, object, object]:
    del workflow_input
    result = yield ctx.wait_for_external_event("complete")
    return result


def main() -> None:
    stopped = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    runtime.start()
    try:
        stopped.wait()
    finally:
        runtime.shutdown()


if __name__ == "__main__":
    main()
