"""Accepted roots become terminal when synchronous task startup fails."""

from __future__ import annotations

import asyncio
from contextlib import closing

import pytest

from tests.support.execution_harness import ExecutionHarness, RecordingRunTracer
from toolang.execution.executor import executor as executor_module
from toolang.base.types.message import TextPart
from toolang.execution.events import RunEnd
from toolang.execution.store import RunStore
from toolang.execution.types import ThreadPrefix


@pytest.mark.parametrize("stage", ["task", "monitor", "observer"])
@pytest.mark.parametrize("message", ["startup failed", ""])
def test_failed_root_startup_is_durable_and_published_once(
    tmp_path, monkeypatch, stage, message
):
    harness = ExecutionHarness.create(
        tmp_path, source="flow root(_: Text) -> Text:\n  pass\n", responses=[]
    )
    failure = RuntimeError(message)
    create_task = asyncio.create_task
    rejected = []

    def fail_startup(*args, **kwargs):
        raise failure

    def fail_task(coro, **kwargs):
        if kwargs.get("name") == (
            "toolang-run-observer" if stage == "observer" else "toolang-run-run_failed"
        ):
            rejected.append(coro)
            raise failure
        return create_task(coro, **kwargs)

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.CHAT)
            tracer = RecordingRunTracer()
            with monkeypatch.context() as patch:
                if stage == "monitor":
                    patch.setattr(harness.executor, "_ensure_monitor", fail_startup)
                else:
                    patch.setattr(asyncio, "create_task", fail_task)
                with pytest.raises(RuntimeError) as caught:
                    harness.executor.run(
                        harness.run_spec(
                            thread=thread, runnable="root", primary=(TextPart("ready"),)
                        ),
                        run_id="run_failed",
                        tracer=tracer,
                    )
            assert caught.value is failure
            assert all(coro.cr_frame is None for coro in rejected)
            assert not harness.executor.has_owned_work()
            observers = tuple(harness.executor._observers)
            if observers:
                await asyncio.wait_for(
                    asyncio.gather(*(item.task for item in observers)), 1
                )
            assert not harness.executor.stream._readers
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
            assert tracer.events == ([] if stage == "observer" else [frame.event])
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

    try:
        asyncio.run(scenario())
    finally:
        for coro in rejected:
            coro.close()


@pytest.mark.parametrize("stage", ["execution", "timer"])
def test_owned_initialization_failure_finishes_without_waiting_for_stop(
    tmp_path, monkeypatch, stage
):
    harness = ExecutionHarness.create(
        tmp_path, source="flow root(_: Text) -> Text:\n  pass\n", responses=[]
    )

    def fail_startup(*args, **kwargs):
        raise RuntimeError("initialization failed")

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.CHAT)
            tracer = RecordingRunTracer()
            with monkeypatch.context() as patch:
                patch.setattr(
                    executor_module._Execution,
                    "__init__" if stage == "execution" else "schedule_time_limit",
                    fail_startup,
                )
                record = await harness.executor.run(
                    harness.run_spec(
                        thread=thread, runnable="root", primary=(TextPart("ready"),)
                    ),
                    tracer=tracer,
                )
            assert record.status == "failed"
            assert record.error is not None
            assert harness.store.resolve_error(record.error) == "initialization failed"
            assert record.end_cursor is not None and record.begin_cursor is None
            assert len(tracer.events) == 1 and isinstance(tracer.events[0], RunEnd)
            await harness.executor.stop()
            with harness.executor.stream.boundary() as (_, _, frames):
                assert [
                    frame.event for frame in frames if frame.root_run_id == record.id
                ] == tracer.events

    asyncio.run(scenario())


@pytest.mark.skipif(
    not hasattr(asyncio, "eager_task_factory"), reason="requires Python 3.12+"
)
def test_eager_task_factory_cannot_start_execution_before_ownership(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path, source="flow root(_: Text) -> Text:\n  pass\n", responses=[]
    )

    async def scenario():
        async with harness:
            loop = asyncio.get_running_loop()
            loop.set_task_factory(getattr(asyncio, "eager_task_factory"))
            try:
                record = await harness.executor.run(
                    harness.run_spec(
                        thread=harness.threads.create(prefix=ThreadPrefix.CHAT),
                        runnable="root",
                        primary=(TextPart("ready"),),
                    )
                )
                assert record.status == "succeeded"
            finally:
                loop.set_task_factory(None)

    asyncio.run(scenario())
