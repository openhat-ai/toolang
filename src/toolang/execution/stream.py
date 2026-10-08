"""Bounded canonical events shared by all observers of one executor."""

from __future__ import annotations

import asyncio
from bisect import bisect_right
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
import threading
from uuid import uuid4

from toolang.common.pubsub import Signal

from .errors import StreamGapError, StreamOverflowError
from .events import (
    ExecutionEvent,
    PartBegin,
    PartDelta,
    PartEnd,
    RunRetried,
    RunTracer,
    StepEnd,
    ThreadCreated,
    ThreadForked,
    ThreadRewound,
    event_to_data,
)
from .types import EventCursor, StepRef

_LOGGER = logging.getLogger(__name__)
OBSERVER_DRAIN_SEC = 1.0


@dataclass(frozen=True, slots=True)
class StreamLimits:
    """Count and serialized-byte budgets, including one acquired reader batch."""

    events: int = 4096
    bytes: int = 16 * 1024 * 1024
    batch_events: int = 128
    batch_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (self.events, self.bytes, self.batch_events, self.batch_bytes)
        ):
            raise ValueError("stream budgets must be positive integers")


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    cursor: EventCursor
    event: ExecutionEvent
    thread_id: str
    root_run_id: str | None
    size: int


@dataclass(frozen=True, slots=True)
class EventBatch:
    events: tuple[CanonicalEvent, ...]
    cursor: EventCursor


class Publication:
    """Stage events inside a durable transaction; expose them only after commit."""

    def __init__(self, source: CanonicalStream) -> None:
        self._source = source
        self._pending: list[CanonicalEvent] = []

    @property
    def cursor(self) -> str:
        return str(
            EventCursor(self._source.epoch, self._source._tail + len(self._pending) + 1)
        )

    def append(
        self,
        event: ExecutionEvent,
        *,
        thread_id: str = "",
        root_run_id: str | None = None,
    ) -> None:
        if isinstance(event, ThreadCreated | ThreadForked | ThreadRewound):
            thread_id = event.thread
        size = len(json.dumps(event_to_data(event), ensure_ascii=False).encode())
        self._pending.append(
            CanonicalEvent(
                EventCursor.parse(self.cursor), event, thread_id, root_run_id, size
            )
        )


class CanonicalStream:
    """Serialize durable projection and publication, never observer callbacks."""

    def __init__(self, *, limits: StreamLimits | None = None) -> None:
        self.limits = limits or StreamLimits()
        self.epoch = uuid4().hex
        self._lock = threading.RLock()
        self._signal = Signal()
        self._events: deque[CanonicalEvent] = deque()
        self._bytes = 0
        self._tail = 0
        self._floor = 0
        self._readers: set[StreamReader] = set()
        self._closed = False
        self._publishing = False

    @property
    def tail(self) -> EventCursor:
        with self._lock:
            return EventCursor(self.epoch, self._tail)

    @property
    def floor(self) -> EventCursor:
        with self._lock:
            return EventCursor(self.epoch, self._floor)

    @property
    def read_floor(self) -> EventCursor:
        with self._lock:
            return EventCursor(self.epoch, self._read_floor())

    def _read_floor(self) -> int:
        return min((reader._seq for reader in self._readers), default=self._tail)

    @contextmanager
    def publication(self) -> Iterator[Publication]:
        """Hold only the projection/commit/publication section, with no awaits."""
        with self._lock:
            if self._closed:
                raise RuntimeError("canonical stream is closed")
            if self._publishing:
                raise RuntimeError("canonical publication cannot be nested")
            publication = Publication(self)
            self._publishing = True
            try:
                yield publication
            finally:
                self._publishing = False
            for frame in publication._pending:
                self._tail = frame.cursor.seq
                self._events.append(frame)
                self._bytes += frame.size
                self._reclaim()
            if publication._pending:
                self._signal.notify()

    def subscribe(
        self,
        *,
        after: EventCursor | None = None,
        root_run_id: str | None = None,
        thread_id: str | None = None,
    ) -> StreamReader:
        with self._lock:
            cursor = after or self.tail
            if cursor.epoch != self.epoch or cursor.seq < self._floor:
                raise StreamGapError("event cursor requires records recovery")
            if cursor.seq > self._tail:
                raise ValueError("event cursor is ahead of the stream")
            reader = StreamReader(self, cursor.seq, root_run_id, thread_id)
            self._readers.add(reader)
            if not self._closed:
                self._signal.connect(reader._notify)
            return reader

    def close(self) -> None:
        """Wake readers; retained events can drain before end-of-stream."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._signal.notify()
                self._signal.close()

    def _over_limit(self) -> bool:
        return len(self._events) > self.limits.events or self._bytes > self.limits.bytes

    def _reclaim(self) -> None:
        if not self._over_limit():
            return
        read_floor = self._read_floor()
        completed: dict[StepRef, list[int]] = {}
        for frame in self._events:
            if isinstance(frame.event, StepEnd):
                completed.setdefault(frame.event.step, []).append(frame.cursor.seq)
        self._compact(completed, read_floor)
        # Drop a blocking reader before sacrificing structural cache coverage.
        # Only finalized Parts can justify releasing retention this way.
        while self._over_limit():
            blocked = (
                ends[index]
                for frame in self._events
                if isinstance(frame.event, PartBegin | PartDelta | PartEnd)
                if (ends := completed.get(frame.event.step))
                if (index := bisect_right(ends, frame.cursor.seq)) < len(ends)
            )
            boundary = min(blocked, default=None)
            if boundary is None:
                break
            for reader in tuple(self._readers):
                if reader._seq < boundary:
                    reader._overflow()
            self._compact(completed, self._read_floor())
        while self._over_limit():
            evicted = self._events.popleft()
            self._bytes -= evicted.size
            self._floor = evicted.cursor.seq
            for reader in tuple(self._readers):
                if reader._seq < self._floor:
                    reader._overflow()

    def _compact(self, completed: dict[StepRef, list[int]], read_floor: int) -> None:
        consumed = {
            step: ends[index - 1]
            for step, ends in completed.items()
            if (index := bisect_right(ends, read_floor))
        }
        retained = deque(
            frame
            for frame in self._events
            if not (
                isinstance(frame.event, PartBegin | PartDelta | PartEnd)
                and frame.cursor.seq < consumed.get(frame.event.step, -1)
            )
        )
        self._events = retained
        self._bytes = sum(frame.size for frame in retained)


class StreamReader:
    """A scan cursor and coalesced wakeup, with no per-event queue."""

    def __init__(
        self,
        source: CanonicalStream,
        seq: int,
        root_run_id: str | None,
        thread_id: str | None,
    ) -> None:
        self._source = source
        self._seq = seq
        self._root = root_run_id
        self._thread = thread_id
        self._loop = asyncio.get_running_loop()
        self._ready = asyncio.Event()
        self._scheduled = False
        self._closed = False
        self._error: StreamOverflowError | None = None
        self._until: int | None = None

    @property
    def cursor(self) -> EventCursor:
        with self._source._lock:
            return EventCursor(self._source.epoch, self._seq)

    def _notify(self) -> None:
        if self._scheduled or self._closed:
            return
        self._scheduled = True
        try:
            self._loop.call_soon_threadsafe(self._wake)
        except RuntimeError:
            self.close()

    def _wake(self) -> None:
        with self._source._lock:
            self._scheduled = False
            self._ready.set()

    def _overflow(self) -> None:
        self._error = StreamOverflowError("canonical event subscription overflow")
        self._source._readers.discard(self)
        self._notify()
        self._source._signal.disconnect(self._notify)

    def finish(self) -> None:
        """Drain through the current tail, then release observation."""
        with self._source._lock:
            if self._until is None:
                self._until = self._source._tail
            if self._seq >= self._until:
                self.close()
            else:
                self._notify()

    def check(self) -> None:
        if self._error is not None:
            raise self._error

    def _matches(self, frame: CanonicalEvent) -> bool:
        if self._root is not None and frame.root_run_id != self._root:
            return False
        return (
            self._thread is None
            or self._thread == frame.thread_id
            or (
                isinstance(frame.event, ThreadForked)
                and self._thread == frame.event.source_thread
            )
        )

    @property
    def empty(self) -> bool:
        with self._source._lock:
            self.check()
            return not any(
                frame.cursor.seq > self._seq and self._matches(frame)
                for frame in self._source._events
            )

    async def receive(self) -> EventBatch:
        # A busy worker publisher or entirely filtered scope must not starve
        # the event loop, cancellation, or other readers. Yield once per batch.
        await asyncio.sleep(0)
        source = self._source
        while True:
            with source._lock:
                self.check()
                if self._closed:
                    raise StopAsyncIteration
                if self._until is not None and self._seq >= self._until:
                    self.close()
                    raise StopAsyncIteration
                if self._seq < source._tail:
                    events: list[CanonicalEvent] = []
                    size = 0
                    scanned = 0
                    for frame in source._events:
                        if frame.cursor.seq <= self._seq:
                            continue
                        if self._until is not None and frame.cursor.seq > self._until:
                            break
                        if scanned >= source.limits.batch_events:
                            break
                        if self._matches(frame):
                            if frame.size > source.limits.batch_bytes:
                                self._overflow()
                                self.check()
                            if size + frame.size > source.limits.batch_bytes:
                                break
                            events.append(frame)
                            size += frame.size
                        self._seq = frame.cursor.seq
                        scanned += 1
                    if not scanned:
                        self._seq = (
                            min(source._tail, self._until)
                            if self._until is not None
                            else source._tail
                        )
                    if self._until is not None and self._seq >= self._until:
                        self.close()
                    return EventBatch(tuple(events), self.cursor)
                if source._closed:
                    self.close()
                    raise StopAsyncIteration
                self._ready.clear()
            await self._ready.wait()

    def close(self) -> None:
        with self._source._lock:
            if self._closed:
                return
            self._notify()
            self._closed = True
            self._source._signal.disconnect(self._notify)
            self._source._readers.discard(self)


class TraceObserver:
    """Run a caller tracer independently, with bounded retention and drain."""

    def __init__(self, reader: StreamReader, tracer: RunTracer) -> None:
        self.reader = reader
        self._deadline: asyncio.TimerHandle | None = None
        self.task = asyncio.create_task(self._run(tracer), name="toolang-run-observer")

    async def _run(self, tracer: RunTracer) -> None:
        try:
            while True:
                batch = await self.reader.receive()
                for frame in batch.events:
                    self.reader.check()
                    if isinstance(
                        frame.event,
                        ThreadCreated | ThreadForked | ThreadRewound | RunRetried,
                    ):
                        continue
                    try:
                        await tracer.on_event(frame.event)
                    except Exception:
                        _LOGGER.exception("run tracer event handling failed")
        except StopAsyncIteration:
            pass
        except StreamOverflowError:
            _LOGGER.exception("run tracer subscription overflow")
        finally:
            self.reader.close()
            if self._deadline is not None:
                self._deadline.cancel()

    def finish(self) -> None:
        self.reader.finish()
        if not self.task.done() and self._deadline is None:
            self._deadline = self.task.get_loop().call_later(
                OBSERVER_DRAIN_SEC, self._expire
            )

    def _expire(self) -> None:
        if not self.task.done():
            _LOGGER.warning("run tracer did not drain before its deadline")
            self.reader.close()
            self.task.cancel()

    async def drain(self) -> None:
        await asyncio.wait({self.task}, timeout=OBSERVER_DRAIN_SEC)
        self._expire()
