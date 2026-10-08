"""Canonical ordering, bounded retention, and independent reader lifetimes."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest

from toolang.base.types.message import TextDelta, TextPart
from toolang.execution.errors import StreamGapError, StreamOverflowError
from toolang.execution.events import (
    PartBegin,
    PartDelta,
    PartEnd,
    RunBegin,
    RunEnd,
    StepEnd,
    ThreadForked,
)
from toolang.execution.stream import CanonicalStream, StreamLimits
from toolang.execution.types import ControlRef, EventCursor, StepRef


def emit(source, event, *, root="run_a", thread="term_a"):
    with source.publication() as publication:
        cursor = EventCursor.parse(publication.cursor)
        publication.append(event, root_run_id=root, thread_id=thread)
    return cursor


def begin(run="run_a"):
    return RunBegin(run=run, control=ControlRef.for_run(run, 0), thread_id="term_a")


def progress():
    step = StepRef.parse("run_a.0")
    return (
        begin(),
        PartBegin(step, 0, "text"),
        PartDelta(step, 0, TextDelta("hello")),
        PartEnd(step, 0, TextPart("hello")),
        StepEnd(step, "value", "succeeded"),
    )


def test_cursor_tokens_are_canonical_and_epochs_are_not_ordered():
    cursor = EventCursor("a" * 32, 15)
    assert EventCursor.parse(str(cursor)) == cursor
    for invalid in (
        "",
        str(cursor).upper(),
        str(cursor)[:-1],
        "b" * 32 + ".-000000000000001",
    ):
        with pytest.raises(ValueError):
            EventCursor.parse(invalid)
    with pytest.raises(TypeError):
        _ = cast(Any, cursor) < EventCursor("b" * 32, 1)


def test_scope_filtering_advances_one_shared_order_and_close_is_independent():
    async def scenario():
        source = CanonicalStream()
        agent = source.subscribe()
        root = source.subscribe(root_run_id="run_a")
        thread = source.subscribe(thread_id="term_a")
        frames = (
            (begin(), "run_a", "term_a"),
            (begin("run_child"), "run_a", "term_a"),
            (begin("run_other"), "run_other", "term_b"),
        )
        for event, run_id, thread_id in frames:
            emit(source, event, root=run_id, thread=thread_id)
        a, b, c = await agent.receive(), await root.receive(), await thread.receive()
        assert [item.cursor.seq for item in a.events] == [1, 2, 3]
        assert [item.cursor.seq for item in b.events] == [1, 2]
        assert b.events == c.events
        assert a.cursor == b.cursor == c.cursor == source.tail
        root.close()
        emit(source, RunEnd("run_a", "succeeded"))
        assert (await agent.receive()).events == (await thread.receive()).events
        source.close()
        with pytest.raises(StopAsyncIteration):
            await agent.receive()
        thread.close()

    asyncio.run(scenario())


def test_fork_reaches_both_thread_scopes_once():
    async def scenario():
        source = CanonicalStream()
        old = source.subscribe(thread_id="term_old")
        new = source.subscribe(thread_id="term_new")
        event = ThreadForked(
            "term_new", ControlRef.for_thread("term_new", 0), "term_old", "run_a", "now"
        )
        emit(source, event)
        assert (await old.receive()).events == (await new.receive()).events
        old.close()
        new.close()

    asyncio.run(scenario())


def test_failed_projection_discards_every_staged_event():
    source = CanonicalStream()
    with pytest.raises(ValueError, match="projection failed"):
        with source.publication() as publication:
            publication.append(begin())
            raise ValueError("projection failed")
    assert source.tail.seq == source.floor.seq == 0
    assert emit(source, begin()).seq == 1


def test_parallel_publishers_share_cursors_and_coalesce_worker_wakeups(monkeypatch):
    async def scenario():
        source = CanonicalStream()
        reader = source.subscribe()
        scheduled = 0
        schedule = reader._loop.call_soon_threadsafe

        def call(*args, **kwargs):
            nonlocal scheduled
            scheduled += 1
            return schedule(*args, **kwargs)

        monkeypatch.setattr(reader._loop, "call_soon_threadsafe", call)
        with ThreadPoolExecutor(max_workers=4) as workers:
            cursors = list(workers.map(lambda _: emit(source, begin()), range(200)))
        assert sorted(cursor.seq for cursor in cursors) == list(range(1, 201))
        assert scheduled == 1
        first, second = await reader.receive(), await reader.receive()
        assert len(first.events) == 128 and len(second.events) == 72
        assert first.cursor.seq == 128 and second.cursor.seq == 200
        reader.close()

    asyncio.run(scenario())


def test_parts_survive_finalization_until_consumed_and_capacity_pressure():
    async def scenario():
        source = CanonicalStream(limits=StreamLimits(events=6))
        first = source.tail
        reader = source.subscribe()
        for event in progress():
            emit(source, event)
        batch = await reader.receive()
        assert len(batch.events) == 5
        replay = source.subscribe(after=first)
        assert (await replay.receive()).events == batch.events
        emit(source, RunEnd("run_a", "succeeded"))
        emit(source, begin("run_b"))
        assert source.floor == first
        replay.close()
        after = source.subscribe(after=first)
        assert [item.cursor.seq for item in (await after.receive()).events] == [
            1,
            5,
            6,
            7,
        ]
        # A batch already acquired by a reader remains intact after compaction.
        assert len(batch.events) == 5
        reader.close()
        after.close()

    asyncio.run(scenario())


def test_lagging_reader_overflows_before_structural_cache_is_evicted():
    async def scenario():
        source = CanonicalStream(limits=StreamLimits(events=6))
        slow = source.subscribe()
        fast = source.subscribe()
        for event in progress():
            emit(source, event)
        await fast.receive()
        emit(source, RunEnd("run_a", "succeeded"))
        assert slow._error is None  # Finalization alone never clears unread Parts.
        emit(source, begin("run_b"))
        with pytest.raises(StreamOverflowError):
            await slow.receive()
        assert source.floor.seq == 0
        assert [item.cursor.seq for item in (await fast.receive()).events] == [6, 7]
        assert slow not in source._readers
        slow.close()
        fast.close()

    asyncio.run(scenario())


def test_evicted_prefix_and_epoch_mismatch_require_records_recovery():
    async def scenario():
        source = CanonicalStream(limits=StreamLimits(events=2))
        old = source.tail
        for _ in range(3):
            emit(source, begin())
        assert source.floor.seq == 1
        for cursor in (old, CanonicalStream().tail):
            with pytest.raises(StreamGapError):
                source.subscribe(after=cursor)
        with pytest.raises(ValueError, match="ahead"):
            source.subscribe(after=EventCursor(source.epoch, 4))
        reader = source.subscribe(after=source.floor)
        assert [item.cursor.seq for item in (await reader.receive()).events] == [2, 3]
        reader.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cache_bytes,batch_bytes", [(10, 1024), (1024, 10)])
def test_oversized_events_fail_observation_without_failing_publication(
    cache_bytes, batch_bytes
):
    async def scenario():
        source = CanonicalStream(
            limits=StreamLimits(bytes=cache_bytes, batch_bytes=batch_bytes)
        )
        reader = source.subscribe()
        assert emit(source, begin()).seq == 1
        with pytest.raises(StreamOverflowError):
            await reader.receive()
        assert source.tail.seq == 1
        reader.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("close_source", [False, True])
def test_close_wakes_idle_readers_and_releases_listeners(close_source):
    async def scenario():
        source = CanonicalStream()
        reader = source.subscribe()
        waiting = asyncio.create_task(reader.receive())
        await asyncio.sleep(0)
        if close_source:
            source.close()
        else:
            reader.close()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(waiting, 1)
        assert not source._signal._listeners
        assert not source._readers
        reader.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("finish_before_receive", [False, True])
def test_finished_reader_keeps_its_acquired_batch_without_pinning_future_events(
    finish_before_receive,
):
    async def scenario():
        source = CanonicalStream(limits=StreamLimits(events=3))
        reader = source.subscribe(root_run_id="run_a")
        emit(source, begin())
        emit(source, RunEnd("run_a", "succeeded"))
        if finish_before_receive:
            reader.finish()
            emit(source, begin("run_b"), root="run_b")
            reader.finish()
        batch = await reader.receive()
        assert batch.cursor.seq == 2
        if not finish_before_receive:
            reader.finish()
        for _ in range(4):
            emit(source, begin("run_b"), root="run_b")
        reader.finish()  # Repeated cleanup must not extend the cutoff.
        reader.check()
        assert [frame.event.type for frame in batch.events] == ["run_begin", "run_end"]
        with pytest.raises(StopAsyncIteration):
            await reader.receive()
        assert source.read_floor == source.tail
        assert not source._signal._listeners

    asyncio.run(scenario())


def test_compaction_of_an_older_attempt_does_not_overflow_a_current_reader():
    async def scenario():
        source = CanonicalStream(limits=StreamLimits(events=7))
        reader = source.subscribe()
        for event in progress():
            emit(source, event)
        await reader.receive()
        # The same Step path now belongs to another attempt. Its new End must
        # not prevent reclaiming the consumed Parts from the preceding attempt.
        emit(source, PartDelta(StepRef.parse("run_a.0"), 0, TextDelta("retry")))
        emit(source, StepEnd(StepRef.parse("run_a.0"), "value", "succeeded"))
        emit(source, RunEnd("run_a", "succeeded"))
        batch = await reader.receive()
        assert [frame.cursor.seq for frame in batch.events] == [6, 7, 8]
        assert source.floor.seq == 0
        reader.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("wait_for_drain", [False, True])
def test_stalled_tracer_has_a_bounded_drain_and_releases_its_reader(
    monkeypatch, caplog, wait_for_drain
):
    from toolang.execution import stream
    from toolang.execution.events import RunTracer

    monkeypatch.setattr(stream, "OBSERVER_DRAIN_SEC", 0.01)
    caplog.set_level("WARNING", logger=stream._LOGGER.name)
    monkeypatch.setattr(stream._LOGGER, "handlers", [caplog.handler])

    async def scenario():
        entered = asyncio.Event()
        canceled = asyncio.Event()

        class StalledTracer(RunTracer):
            async def on_event(self, event):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    canceled.set()

        source = CanonicalStream()
        observer = stream.TraceObserver(source.subscribe(), StalledTracer())
        healthy = source.subscribe()
        emit(source, begin())
        await asyncio.wait_for(entered.wait(), 1)
        assert (await healthy.receive()).events[0].event == begin()
        observer.finish()
        if wait_for_drain:
            await asyncio.wait_for(observer.drain(), 1)
        await asyncio.wait_for(canceled.wait(), 1)
        assert observer.reader not in source._readers
        assert "did not drain" in caplog.text
        healthy.close()
        assert not source._signal._listeners

    asyncio.run(scenario())
