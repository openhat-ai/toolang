"""Optional asynchronous canonical-stream export, independent of execution."""

from __future__ import annotations

import asyncio
from hashlib import sha256
import logging
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from toolang.execution.events import (
    RunEnd,
    StepEnd,
    ThreadCreated,
    ThreadForked,
    ThreadRewound,
)
from toolang.execution.events import RunBegin, StepBegin, event_from_data, event_to_data
from toolang.execution.errors import StreamGapError, StreamOverflowError
from toolang.execution.observation import SNAPSHOT_SECONDS
from toolang.execution.errors import SnapshotLimitError
from toolang.execution.records import RunRecord, StepRecord
from toolang.execution.store import RunStore
from toolang.execution.stream import CanonicalStream, CanonicalEvent, StreamReader
from toolang.execution.subscriptions import RecordSnapshot, record_controls
from toolang.execution.types import EventCursor
from .errors import BackendUnavailable, EventProtocolError, EventRecoveryRequired
from .types import EventPublisher
from .events import MAX_EVENT_BYTES, PARTS, encode
from .records import MAX_BYTES, MAX_RECENT, EventProjection, field

logger = logging.getLogger(__name__)


def _read_projection(store: RunStore, boundary: EventCursor) -> EventProjection:
    """Load active structure first; optional history cannot evict an active tree."""
    projection = EventProjection()
    # Leave time to finish encoding/trimming and return the active snapshot.
    # Optional trees share this deadline instead of each getting five seconds.
    history_deadline = time.monotonic() + SNAPSHOT_SECONDS / 2

    def add(
        runs: list[RunRecord], steps: list[StepRecord], *, deadline: float | None = None
    ) -> None:
        snapshot = RecordSnapshot(store, runs, steps, deadline=deadline)
        roots: dict[str, str] = {}
        threads: dict[str, str] = {}
        for frame in snapshot.structural():
            if deadline is not None and time.monotonic() >= deadline:
                raise SnapshotLimitError("Optional history snapshot expired")
            event = event_from_data(frame.data)
            if isinstance(event, RunBegin):
                roots[event.run] = (
                    roots[event.parent.run_id] if event.parent else event.run
                )
                threads[event.run] = event.thread_id
            assert isinstance(event, RunBegin | RunEnd | StepBegin | StepEnd)
            run = (
                event.run if isinstance(event, RunBegin | RunEnd) else event.step.run_id
            )
            # Legacy and retry-admission records retain null Begin cursors.
            cursor = (
                EventCursor.parse(frame.data["cursor"])
                if frame.data.get("cursor")
                else boundary
            )
            projection.apply(CanonicalEvent(cursor, event, threads[run], roots[run], 0))
            identity = (
                event.run if isinstance(event, RunBegin | RunEnd) else str(event.step)
            )
            entity = projection.entities[
                field(
                    "run" if isinstance(event, RunBegin | RunEnd) else "step", identity
                )
            ]
            entity[
                "begin_source"
                if isinstance(event, RunBegin | StepBegin)
                else "end_source"
            ] = frame.data.get("cursor")
        projection.trim()

    runs, steps = store.stream_records(
        root=None, thread=None, after=None, complete=False
    )
    add(runs, steps)
    active = {run.id for run in runs}
    # Thread controls are optional history as well. Resolve them before the
    # optional roots consume the remaining private SQLite read budget.
    try:
        for frame in record_controls(store, recent=True):
            if time.monotonic() >= history_deadline:
                break
            event = event_from_data(frame.data)
            assert isinstance(event, ThreadCreated | ThreadForked | ThreadRewound)
            projection.apply(
                CanonicalEvent(
                    EventCursor.parse(frame.data["cursor"])
                    if frame.data.get("cursor")
                    else boundary,
                    event,
                    event.thread,
                    None,
                    0,
                )
            )
            projection.entities[field("control", str(event.control))]["source"] = (
                frame.data.get("cursor")
            )
        for root in store.stream_recent_roots(MAX_RECENT):
            if time.monotonic() >= history_deadline:
                break
            if root in active:
                continue
            before = projection.copy()
            try:
                add(
                    *store.stream_records(
                        root=root, thread=None, after=None, complete=True
                    ),
                    deadline=history_deadline,
                )
            except (ValueError, sqlite3.Error, SnapshotLimitError):
                projection = before
                break
    except (ValueError, sqlite3.Error, SnapshotLimitError):
        pass
    projection.trim()
    return projection


class EventExporter:
    def __init__(
        self,
        source: CanonicalStream,
        path: Path,
        backend: EventPublisher,
        *,
        agent: str,
        token: str,
    ) -> None:
        self.source, self.path, self.backend, self.agent, self.token = (
            source,
            path,
            backend,
            agent,
            token,
        )
        # Install before any producer is started, without backend I/O.
        self.reader = source.subscribe()
        self.projection = EventProjection()
        self._projection_bytes = 0
        self.epoch = ""
        self.generation = ""
        self.highwater = self.reader.cursor
        self._stopping = False

    async def _retry(self, call, *args):
        delay = 0.5
        while True:
            try:
                return await call(*args)
            except BackendUnavailable:
                # Retain this exact operation until its uncertain result is known.
                await asyncio.sleep(delay)
                delay = min(5, delay * 2)

    async def _snapshot(self) -> tuple[EventCursor, EventProjection, StreamReader]:
        opening = asyncio.create_task(
            asyncio.to_thread(RunStore, self.path, read_only=True)
        )
        try:
            store = await asyncio.shield(opening)
        except BaseException:
            opening.add_done_callback(
                lambda task: (
                    task.result().close()
                    if not task.cancelled() and task.exception() is None
                    else None
                )
            )
            raise
        reader = None
        try:
            with self.source.boundary() as (boundary, _, _):
                store.pin_stream_snapshot(seconds=SNAPSHOT_SECONDS, max_bytes=MAX_BYTES)
                reader = self.source.subscribe(after=boundary)

            def read():
                try:
                    return _read_projection(store, boundary)
                finally:
                    store.close()

            worker = asyncio.create_task(asyncio.to_thread(read))
            try:
                projection = await asyncio.wait_for(
                    asyncio.shield(worker), SNAPSHOT_SECONDS
                )
            except BaseException:
                store.expire_stream_snapshot()
                worker.add_done_callback(
                    lambda task: None if task.cancelled() else task.exception()
                )
                raise
            reader.check()
            return boundary, projection, reader
        except BaseException as exc:
            if reader is not None:
                reader.close()
            else:
                store.close()
            if isinstance(exc, (ValueError, sqlite3.Error, TimeoutError)):
                raise SnapshotLimitError("Records snapshot exceeds budget") from exc
            raise

    async def recover(self, reason: str) -> None:
        meta = await self.backend.initialize()
        self.epoch = meta["epoch"]
        _, origins, _ = await self.backend.capture(self.agent)
        old = origins.get(self.agent, {})
        if old.get("staging"):
            await self.backend.abandon(self.agent, old["staging"], token=self.token)
        generation, recovery = uuid4().hex, uuid4().hex
        base = dict(
            v=1,
            epoch=self.epoch,
            agent=self.agent,
            token=self.token,
            generation=generation,
            recovery=recovery,
        )
        await self._retry(
            self.backend.commit,
            {
                **base,
                "kind": "incomplete",
                "reason": reason,
                "id": f"{recovery}:incomplete",
            },
        )
        boundary, projection, reader = await self._snapshot()
        try:
            entities = {
                key: encode(value) for key, value in projection.entities.items()
            }
            digest = sha256(encode(entities).encode()).hexdigest()
            await self._retry(
                self.backend.stage,
                {
                    **base,
                    "source": str(boundary),
                    "entities": entities,
                    "digest": digest,
                },
            )
            reader.check()
            await self._retry(
                self.backend.commit,
                {
                    **base,
                    "kind": "recover",
                    "id": f"{recovery}:commit",
                    "source": str(boundary),
                    "source_epoch": boundary.epoch,
                    "snapshot_digest": digest,
                    "old_generation": old.get("generation", generation),
                },
            )
            reader.check()
        except BaseException:
            reader.close()
            raise
        self.reader.close()
        self.reader = reader
        self.highwater = boundary
        self.generation = generation
        self.projection = projection
        self._projection_bytes = len(encode(projection.entities).encode()) + 128 * len(
            projection.entities
        )
        if self._stopping:
            self.reader.finish()

    async def publish(self, frame: CanonicalEvent) -> None:
        data = encode(
            dict(
                v=1,
                source_cursor=str(frame.cursor),
                thread=frame.thread_id,
                root=frame.root_run_id,
                event=event_to_data(frame.event),
            )
        )
        if len(data.encode()) + len(self.agent.encode()) + 133 > MAX_EVENT_BYTES:
            raise EventRecoveryRequired("oversized event")
        candidate = (
            self.projection
            if isinstance(frame.event, PARTS)
            else self.projection.copy()
        )
        structural = candidate.apply(frame)
        evicted = candidate.trim() if structural else False
        updates = {
            key: encode(value)
            for key, value in candidate.entities.items()
            if structural and self.projection.entities.get(key) != value
        }
        removed = list(self.projection.entities.keys() - candidate.entities.keys())
        size = (
            len(encode(candidate.entities).encode()) + 128 * len(candidate.entities)
            if structural
            else self._projection_bytes
        )
        op = dict(
            v=1,
            epoch=self.epoch,
            agent=self.agent,
            token=self.token,
            generation=self.generation,
            kind="event",
            id=str(frame.cursor),
            source=str(frame.cursor),
            source_epoch=frame.cursor.epoch,
            prior=str(self.highwater),
            data=data,
            updates=updates,
            removed=removed,
            structural=structural,
            evicted=evicted,
            count=len(candidate.entities),
            bytes=size,
        )
        sid = await self._retry(self.backend.commit, op)
        for key in updates:
            candidate.entities[key]["delivery"] = sid
        self.highwater = frame.cursor
        self.projection = candidate
        self._projection_bytes = size

    async def run(self) -> None:
        reason = "initial"
        delay = 0.5
        while True:
            try:
                await self.recover(reason)
                delay = 0.5
                while True:
                    try:
                        batch = await asyncio.wait_for(self.reader.receive(), 1)
                    except TimeoutError:
                        # Detect an empty backend reset even while the agent is idle.
                        meta = await self.backend.initialize()
                        if meta["epoch"] != self.epoch:
                            raise EventRecoveryRequired("backend reset")
                        continue
                    for frame in batch.events:
                        if self._stopping:
                            # Execution has stopped. Recover its final structure
                            # once instead of spending the drain budget on backlog.
                            await self.recover("shutdown")
                            return
                        await self.publish(frame)
            except StopAsyncIteration:
                return
            except EventProtocolError:
                logger.exception("Event dataset requires repair; export stopped")
                return
            except asyncio.CancelledError:
                raise
            except (
                EventRecoveryRequired,
                StreamGapError,
                StreamOverflowError,
                SnapshotLimitError,
                BackendUnavailable,
                TimeoutError,
            ) as exc:
                reason = (
                    "snapshot_limit"
                    if isinstance(exc, SnapshotLimitError)
                    else "source_gap"
                )
                logger.warning("Event export recovering: %s", exc)
                await asyncio.sleep(delay)
                delay = min(5, delay * 2)

    def finish(self) -> None:
        self._stopping = True
        self.reader.finish()

    def close(self) -> None:
        self.reader.close()
