"""Durable canonical boundaries across run trees and thread mutations."""

import asyncio
import gc
import weakref

import pytest

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.execution.events import (
    RunBegin,
    RunEnd,
    StepBegin,
    StepEnd,
    ThreadCreated,
    ThreadForked,
    ThreadRewound,
)
from toolang.execution.store import RunStore
from toolang.execution.stream import CanonicalStream, Publication, StreamLimits
from toolang.execution.threads import ThreadManager
from toolang.execution.types import Pointer, ThreadPrefix


async def collect(reader):
    reader.finish()
    frames = []
    while True:
        try:
            frames.extend((await reader.receive()).events)
        except StopAsyncIteration:
            return frames


def test_parallel_trees_and_thread_mutations_publish_committed_boundaries(
    tmp_path, monkeypatch
):
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent(_: Text) -> Text:
  let one = async run child
  let two = async run child
  await one
  await two
flow child(_: Text) -> Text:
  let result = Done
""",
        responses=[],
    )
    source = harness.executor.stream
    threads = ThreadManager(harness.store, harness.ids, stream=source)
    notified = []
    notify = source._signal.notify

    def inspect_publication():
        # A separate connection observes commits at the publication boundary,
        # including writes from thread operations outside the event loop.
        store = RunStore(harness.store.db_path, read_only=True)
        try:
            for frame in source._events:
                if frame.cursor.seq <= len(notified):
                    continue
                event = frame.event
                if isinstance(event, RunBegin | RunEnd):
                    record = store.get_run(run_id=event.run)
                    assert record is not None and str(record.thread) == frame.thread_id
                    if isinstance(event, RunBegin):
                        assert event.thread_id == frame.thread_id
                elif isinstance(event, StepBegin | StepEnd):
                    record = store.get_step(ref=event.step)
                elif isinstance(event, ThreadCreated | ThreadForked | ThreadRewound):
                    record = store.get_record(Pointer(event.control))
                else:
                    record = None
                if record is not None:
                    field = (
                        "begin_cursor"
                        if isinstance(event, RunBegin | StepBegin)
                        else "end_cursor"
                        if isinstance(event, RunEnd | StepEnd)
                        else "event_cursor"
                    )
                    assert getattr(record, field) == str(frame.cursor)
                notified.append(frame)
        finally:
            store.close()
        notify()

    monkeypatch.setattr(source._signal, "notify", inspect_publication)

    async def scenario():
        async with harness:
            reader = source.subscribe()
            thread = await asyncio.to_thread(threads.create, prefix=ThreadPrefix.CHAT)
            other = threads.create(prefix=ThreadPrefix.CHAT)
            third = threads.create(prefix=ThreadPrefix.CHAT)
            handles = [
                harness.executor.run(
                    harness.run_spec(
                        thread=scope, runnable="parent", primary=(TextPart("input"),)
                    )
                )
                for scope in (thread, other, third)
            ]
            roots = await asyncio.gather(*handles)
            assert all(root.status == "succeeded" for root in roots)
            fork = await asyncio.to_thread(threads.fork, thread_id=thread)
            await asyncio.to_thread(threads.rewind, thread_id=fork)
            frames = await collect(reader)
            assert frames == notified
            assert [f.cursor.seq for f in frames] == list(range(1, len(frames) + 1))
            begins = [f for f in frames if isinstance(f.event, RunBegin)]
            assert len(begins) == 9
            assert {f.root_run_id for f in begins} == {root.id for root in roots}
            assert len({f.thread_id for f in begins}) == 3
            assert len({f.cursor.epoch for f in frames}) == 1

    asyncio.run(scenario())
    assert CanonicalStream().epoch != source.epoch


def test_failed_publication_rolls_back_thread_and_cursor(tmp_path, monkeypatch):
    harness = ExecutionHarness.create(
        tmp_path, source="flow parent:\n  let result = Done\n", responses=[]
    )
    source = harness.executor.stream
    threads = ThreadManager(harness.store, harness.ids, stream=source)
    append = Publication.append

    def fail_after_staging(self, event, **kwargs):
        append(self, event, **kwargs)
        raise RuntimeError("serialization failed")

    with monkeypatch.context() as patch:
        patch.setattr(Publication, "append", fail_after_staging)
        with pytest.raises(RuntimeError, match="serialization failed"):
            threads.create(prefix=ThreadPrefix.CHAT)
    assert not harness.store.list_threads()
    assert not harness.store.list_controls()
    assert source.tail.seq == 0
    threads.create(prefix=ThreadPrefix.CHAT)
    assert source.tail.seq == 1
    harness.store.close()


def test_failing_tracer_cannot_change_run_result(tmp_path, caplog):
    harness = ExecutionHarness.create(
        tmp_path, source="flow parent:\n  let result = Done\n", responses=[]
    )

    class FailingTracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            raise RuntimeError("observer failed")

    async def scenario():
        async with harness:
            tracer = FailingTracer()
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.CHAT),
                    runnable="parent",
                    primary=(TextPart("input"),),
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded"
            assert isinstance(tracer.events[0], RunBegin)
            assert isinstance(tracer.events[-1], RunEnd)
            assert "run tracer event handling failed" in caplog.text

    asyncio.run(scenario())


def test_overflowed_tracer_is_released_while_execution_continues(tmp_path):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic reply:\n  user: Work\n",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("done")), gate=gate
            )
        ],
    )
    harness.executor.stream.limits = StreamLimits(batch_bytes=1)

    async def scenario():
        async with harness:
            tracer = RecordingRunTracer()
            reference = weakref.ref(tracer)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.CHAT),
                    runnable="reply",
                    primary=(TextPart("input"),),
                ),
                tracer=tracer,
            )
            del tracer
            await asyncio.wait_for(gate.wait_until_entered(), 5)
            assert handle.observer is not None
            await asyncio.wait_for(handle.observer.task, 5)
            assert not handle.task.done()
            gc.collect()
            assert reference() is None
            gate.release()
            assert (await handle).status == "succeeded"

    asyncio.run(scenario())
