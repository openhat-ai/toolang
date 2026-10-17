"""Accepted roots become terminal when synchronous task startup fails."""

from __future__ import annotations

import asyncio
from contextlib import closing

import pytest

from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import TextPart
from toolang.execution.events import RunEnd
from toolang.execution.store import RunStore
from toolang.execution.types import ThreadPrefix


@pytest.mark.parametrize("stage", ["dispatch", "monitor"])
@pytest.mark.parametrize("message", ["startup failed", ""])
def test_failed_root_startup_is_durable_and_published_once(
    tmp_path, monkeypatch, stage, message
):
    harness = ExecutionHarness.create(
        tmp_path, source="flow root(_: Text) -> Text:\n  pass\n", responses=[]
    )
    failure = RuntimeError(message)

    def fail_startup(*args, **kwargs):
        raise failure

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.CHAT)
            with monkeypatch.context() as patch:
                patch.setattr(
                    harness.executor,
                    "_launch" if stage == "dispatch" else "_ensure_monitor",
                    fail_startup,
                )
                with pytest.raises(RuntimeError) as caught:
                    harness.executor.run(
                        harness.run_spec(
                            thread=thread, runnable="root", primary=(TextPart("ready"),)
                        ),
                        run_id="run_failed",
                    )
            assert caught.value is failure
            assert not harness.executor.has_owned_work()
            await asyncio.sleep(0)
            with closing(RunStore(harness.store.db_path, read_only=True)) as saved:
                record = saved.get_run(run_id="run_failed")
                assert record is not None and record.status == "failed"
                assert record.error is not None
                assert saved.resolve_error(record.error) == (message or "RuntimeError")
                assert record.finished_at is not None and record.end_cursor is not None
                assert record.begin_cursor is None and not saved.list_steps(
                    run_id=record.id
                )
            with harness.executor.stream.boundary() as (_, _, frames):
                failed = [frame for frame in frames if frame.root_run_id == record.id]
            assert len(failed) == 1
            frame = failed[0]
            assert isinstance(frame.event, RunEnd) and frame.event.status == "failed"
            assert frame.event.error == (message or "RuntimeError")
            assert frame.thread_id == thread and str(frame.cursor) == record.end_cursor
            following = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="root", primary=(TextPart("ready"),)
                )
            )
            assert following.status == "succeeded", (
                harness.store.resolve_error(following.error)
                if following.error
                else None
            )
            await harness.executor.stop()
            with harness.executor.stream.boundary() as (_, _, frames):
                assert [
                    frame for frame in frames if frame.root_run_id == record.id
                ] == failed
            assert not harness.adapter.invocations

    asyncio.run(scenario())
