"""Foreground finalization drains owned work and retains cancellation provenance."""

import asyncio

import pytest

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult
from toolang.execution.types import ThreadKind


def test_shutdown_drains_work_when_cancel_control_persistence_fails(
    tmp_path, monkeypatch
):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic main() -> Text:\n  user: Wait.\n",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("done")), gate=gate
            )
        ],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.EXEC), runnable="main"
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            original = harness.store.accept_run_control

            def fail_cancel(**kwargs):
                if kwargs["kind"] == "cancel":
                    raise OSError("cancel control storage unavailable")
                return original(**kwargs)

            monkeypatch.setattr(harness.store, "accept_run_control", fail_cancel)
            try:
                with pytest.raises(OSError, match="cancel control storage unavailable"):
                    await harness.executor.stop(
                        reason="foreground owner channel closed"
                    )
                assert handle.task.done(), (
                    "shutdown returned with owned execution still active"
                )
                record = harness.store.get_run(run_id=handle.run_id)
                assert record is not None and record.status == "canceled"
            finally:
                gate.release()

    asyncio.run(scenario())
