"""Accepted roots become terminal when synchronous task startup fails."""

from __future__ import annotations

import asyncio
from contextlib import closing

import pytest

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
)
from toolang.execution.executor import executor as executor_module
from toolang.base.types.message import TextPart
from toolang.execution.events import RunEnd
from toolang.execution.store import RunStore
from toolang.execution.types import ErrorMessage, RunReport, ThreadKind


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

    def fail_task(coro, **kwargs):
        name = kwargs.get("name", "")
        if (
            name == "toolang-run-observer"
            if stage == "observer"
            else name.startswith("toolang-controls-")
            if stage == "monitor"
            else name == "toolang-run-run_failed"
        ):
            rejected.append(coro)
            raise failure
        return create_task(coro, **kwargs)

    async def scenario():
        async with harness:
            thread = harness.threads.create(kind=ThreadKind.CHAT)
            tracer = RecordingRunTracer()
            with monkeypatch.context() as patch:
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
                assert record.finished_at is not None and record._end_cursor is not None
                assert record._begin_cursor is None and not saved.list_steps(
                    run_id=record.id
                )
            with harness.executor.stream.boundary() as (_, _, frames):
                failed = [frame for frame in frames if frame.root_run_id == record.id]
            assert len(failed) == 1
            frame = failed[0]
            assert isinstance(frame.event, RunEnd) and frame.event.status == "failed"
            assert frame.event.error == (message or "RuntimeError")
            assert frame.thread_id == thread and str(frame.cursor) == record._end_cursor
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
            thread = harness.threads.create(kind=ThreadKind.CHAT)
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
            assert record._end_cursor is not None and record._begin_cursor is None
            assert len(tracer.events) == 1 and isinstance(tracer.events[0], RunEnd)
            await harness.executor.stop()
            with harness.executor.stream.boundary() as (_, _, frames):
                assert [
                    frame.event for frame in frames if frame.root_run_id == record.id
                ] == tracer.events

    asyncio.run(scenario())


@pytest.mark.parametrize("timing", ["immediate", "next_step", "next_call"])
@pytest.mark.parametrize("shutdown", ["task", "stop"])
def test_pre_entry_cancellation_applies_pending_cancel_and_preserves_reason(
    tmp_path, timing, shutdown
):
    harness = ExecutionHarness.create(
        tmp_path, source="flow root() -> Text:\n  pass\n", responses=[]
    )

    async def scenario():
        async with harness:
            tracer = RecordingRunTracer()
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.CHAT),
                    runnable="root",
                ),
                tracer=tracer,
            )
            control = handle.cancel(reason="requested before execution", timing=timing)
            if shutdown == "stop":
                await harness.executor.stop()
            else:
                handle.task.cancel()
            record = await handle
            saved_control = harness.store.get_run_control(
                run_id=handle.run_id, index=control.index
            )
            assert saved_control is not None and saved_control.status == "applied"
            assert record.status == "canceled" and record.report == RunReport()
            assert record.error == ErrorMessage("requested before execution")
            assert record._begin_cursor is None and record._end_cursor is not None
            assert len(tracer.events) == 1
            terminal = tracer.events[0]
            assert isinstance(terminal, RunEnd) and terminal.status == "canceled"
            assert terminal.control is not None
            assert (terminal.control.run, terminal.control.index) == (
                handle.run_id,
                control.index,
            )
            assert terminal.error == "requested before execution"
            assert not harness.executor.has_owned_work()
            assert not harness.executor.stream._readers
            assert not harness.store.list_steps(run_id=record.id)
            assert not harness.adapter.invocations
            await harness.executor.stop()
            with harness.executor.stream.boundary() as (_, _, frames):
                assert [frame.event for frame in frames] == [terminal]

    asyncio.run(scenario())


def test_startup_interruption_reads_ancestry_without_holding_registry(
    tmp_path, monkeypatch
):
    harness = ExecutionHarness.create(
        tmp_path, source="flow root() -> Text:\n  pass\n", responses=[]
    )
    ancestry = harness.store.run_ancestry
    reads = []

    def read_ancestry(**facts):
        assert not harness.executor._runs_lock.locked()
        reads.append(facts["run_id"])
        return ancestry(**facts)

    async def interrupted(execution, binding, runnable, **kwargs):
        control = harness.executor.cancel(
            run_id=binding.run_id, reason="startup interrupted"
        )
        execution._root_run.interruptions[binding.run_id] = control
        raise asyncio.CancelledError()

    async def scenario():
        async with harness:
            with monkeypatch.context() as patch:
                patch.setattr(harness.store, "run_ancestry", read_ancestry)
                patch.setattr(executor_module._Execution, "execute", interrupted)
                record = await harness.executor.run(
                    harness.run_spec(
                        thread=harness.threads.create(kind=ThreadKind.CHAT),
                        runnable="root",
                    )
                )
            assert reads == [record.id]
            assert record.status == "canceled" and record.report == RunReport()
            assert record.error == ErrorMessage("startup interrupted")

    asyncio.run(scenario())


@pytest.mark.parametrize("launch", ["async run", "spawn"])
def test_repeated_stop_cancellation_drains_owned_and_independent_children(
    tmp_path, monkeypatch, launch
):
    from tests.support.execution_harness import ScriptedModelTurn
    from tests.support.execution_assertions import assert_run_event_integrity
    from toolang.base.types.message import Message
    from toolang.base.types.run import ModelCallResult
    from toolang.execution.stream import TraceObserver
    from toolang.execution.types import CallCounters

    caller_gate, child_gate, finishing = AsyncGate(), AsyncGate(), AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
flow parent() -> Text:
  let job = {launch} child
  run blocker
agic blocker() -> Text:
  user: Caller
agic child() -> Text:
  user: Child
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("caller")), gate=caller_gate
            ),
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("child")), gate=child_gate
            ),
        ],
    )
    finish = executor_module._Execution.finish_run

    async def scenario():
        async with harness:
            tracer = RecordingRunTracer()
            observer = TraceObserver(harness.executor.stream.subscribe(), tracer)
            parent = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.CHAT),
                    runnable="parent",
                )
            )

            async def pause_child(execution, **facts):
                if facts["run"] != parent.run_id:
                    await finishing.wait()
                return await finish(execution, **facts)

            await asyncio.wait_for(
                asyncio.gather(
                    caller_gate.wait_until_entered(), child_gate.wait_until_entered()
                ),
                3,
            )
            with monkeypatch.context() as patch:
                patch.setattr(executor_module._Execution, "finish_run", pause_child)
                stopping = asyncio.create_task(harness.executor.stop())
                await asyncio.wait_for(finishing.wait_until_entered(), 3)
                stopping.cancel()
                await asyncio.sleep(0)
                stopping.cancel()
                finishing.release()
                with pytest.raises(asyncio.CancelledError):
                    await stopping
            await observer.drain()
            runs = harness.store.list_runs(limit=None)
            assert all(run.status == "canceled" for run in runs)
            assert all(
                step.status != "running"
                for run in runs
                for step in harness.store.list_steps(run_id=run.id)
            )
            root = harness.store.get_run(run_id=parent.run_id)
            assert root is not None and root.report is not None
            assert root.report.calls == CallCounters(
                2 if launch == "async run" else 1, 0
            )
            assert not harness.executor._active_runs and not harness.executor._root_runs
            assert_run_event_integrity(tracer.events)

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
                        thread=harness.threads.create(kind=ThreadKind.CHAT),
                        runnable="root",
                        primary=(TextPart("ready"),),
                    )
                )
                assert record.status == "succeeded"
            finally:
                loop.set_task_factory(None)

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["runs", "observers"])
def test_canceling_stop_still_finishes_runs_and_drains_terminal_events(
    tmp_path, monkeypatch, phase
):
    harness = ExecutionHarness.create(
        tmp_path, source="flow root(_: Text) -> Text:\n  pass\n", responses=[]
    )
    executing, finishing, observing = AsyncGate(), AsyncGate(), AsyncGate()
    finish_run = executor_module._Execution.finish_run
    completed = []

    async def wait_for_cancel(self, *args, **kwargs):
        await executing.wait()

    async def pause_finish(self, **facts):
        await finishing.wait()
        result = await finish_run(self, **facts)
        completed.append(facts["run"])
        return result

    monkeypatch.setattr(executor_module._Execution, "execute", wait_for_cancel)
    monkeypatch.setattr(executor_module._Execution, "finish_run", pause_finish)

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if phase == "observers":
                await observing.wait()

    async def scenario():
        async with harness:
            tracer = Tracer()
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.CHAT),
                    runnable="root",
                    primary=(TextPart("ready"),),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(executing.wait_until_entered(), 2)
            if phase == "observers":
                finishing.release()
            stopping = asyncio.create_task(harness.executor.stop())
            await asyncio.wait_for(
                (finishing if phase == "runs" else observing).wait_until_entered(), 2
            )
            if phase == "observers":
                async with asyncio.timeout(2):
                    while not harness.executor.stream._closed:
                        await asyncio.sleep(0)
            stopping.cancel()
            finishing.release()
            with pytest.raises(asyncio.CancelledError):
                await stopping
            assert handle.run_id in completed
            record = harness.store.get_run(run_id=handle.run_id)
            assert record is not None and record.status == "canceled"
            assert harness.executor._monitor_task is None
            assert harness.executor.stream._closed
            assert not harness.executor.stream._readers
            assert not harness.executor._active_runs and not harness.executor._root_runs
            with harness.executor.stream.boundary() as (_, _, frames):
                ended = [
                    frame.event for frame in frames if frame.root_run_id == record.id
                ]
            assert len(ended) == 1 and isinstance(ended[0], RunEnd)
            assert tracer.events == ended
            await harness.executor.stop()
            with harness.executor.stream.boundary() as (_, _, frames):
                assert [
                    frame.event for frame in frames if frame.root_run_id == record.id
                ] == ended

    asyncio.run(scenario())


def test_stop_skips_a_finished_monitor_from_a_closed_owner_loop(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path, source="flow root(_: Text) -> Text:\n  pass\n", responses=[]
    )

    async def run():
        return await harness.executor.run(
            harness.run_spec(
                thread=harness.threads.create(kind=ThreadKind.CHAT),
                runnable="root",
                primary=(TextPart("ready"),),
            )
        )

    try:
        record = asyncio.run(run())
        monitor = harness.executor._monitor_task
        assert monitor is not None and monitor.done() and monitor.get_loop().is_closed()
        asyncio.run(harness.executor.stop())
        assert harness.executor._monitor_task is None
        assert harness.executor.stream._closed
        assert harness.store.get_run(run_id=record.id) == record
    finally:
        harness.store.close()
