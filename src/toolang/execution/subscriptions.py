"""Transport-neutral attachment, structural recovery, and bounded live delivery."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Literal

from .errors import StreamGapError, StreamOverflowError
from .events import (
    ExecutionEvent,
    PartBegin,
    PartDelta,
    PartEnd,
    RunBegin,
    RunEnd,
    RunRetried,
    StepBegin,
    StepEnd,
    ThreadCreated,
    ThreadForked,
    ThreadRewound,
    event_to_data,
)
from .records import CreateControlPayload, ForkControlPayload, RewindControlPayload
from .records import RunRecord, StepRecord, RunControlPayload, StoredModelStepGiven
from .schemas import STREAM_PREFILL_MAX_BYTES, StreamFrame
from .store import RunStore
from .stream import CanonicalEvent, CanonicalStream, StreamReader
from .types import ErrorMessage, EventCursor, ModelStepGiven, StepRef

SNAPSHOT_SECONDS = 5.0
SNAPSHOT_BYTES = STREAM_PREFILL_MAX_BYTES
OPEN_ENTITIES = 4096
_TERMINAL = {"succeeded", "failed", "canceled"}
_PARTS = (PartBegin, PartDelta, PartEnd)


class SnapshotLimitError(RuntimeError):
    """Structural recovery exceeded its bounded read or output budget."""


@dataclass(frozen=True, slots=True)
class StreamScope:
    root: str | None = None
    thread: str | None = None

    def __post_init__(self) -> None:
        if self.root is not None and self.thread is not None:
            raise ValueError("select one stream scope")

    def matches(self, frame: CanonicalEvent) -> bool:
        return (self.root is None or self.root == frame.root_run_id) and (
            self.thread is None
            or self.thread == frame.thread_id
            or isinstance(frame.event, ThreadForked)
            and self.thread == frame.event.source_thread
        )

    def data(self) -> dict[str, str]:
        if self.root is not None:
            return {"kind": "run", "id": self.root}
        if self.thread is not None:
            return {"kind": "thread", "id": self.thread}
        return {"kind": "agent"}


class _Budget:
    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.bytes = 0

    def take(self, data: dict[str, Any]) -> None:
        self.bytes += len(json.dumps(data, ensure_ascii=False).encode())
        if self.bytes > SNAPSHOT_BYTES or time.monotonic() > self.deadline:
            raise SnapshotLimitError("stream snapshot budget exceeded")


class _Frames(list[StreamFrame]):
    """Check serialized size while building, not after allocating the prefix."""

    def __init__(self, deadline: float) -> None:
        super().__init__()
        self.budget = _Budget(deadline)

    def append(self, frame: StreamFrame) -> None:
        self.budget.take(frame.data)
        super().append(frame)

    def extend(self, frames: Iterable[StreamFrame]) -> None:
        for frame in frames:
            self.append(frame)


class Subscriptions:
    """Prepare attachments without making persistence or execution wait for clients."""

    def __init__(self, source: CanonicalStream, path: Path) -> None:
        self.source = source
        self.path = path
        self._attachments: set[Attachment] = set()

    def validate(self, after: str | None) -> EventCursor | None:
        cursor = EventCursor.parse(after) if after is not None else None
        tail = self.source.tail
        if cursor is not None and cursor.epoch == tail.epoch and cursor.seq > tail.seq:
            raise ValueError("event cursor is ahead of the stream")
        return cursor

    async def reserve(self, after: str | None = None) -> Attachment:
        """Validate before POST admission; callers must close unused reservations."""
        cursor = self.validate(after)
        # Opening/validating the schema can block. The first snapshot read below
        # is the only database operation in the publication gate.
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
        admission = self.source.subscribe()
        attachment = Attachment(self.source, store, cursor, admission)
        self._attachments.add(attachment)
        attachment.on_close = lambda: self._attachments.discard(attachment)
        return attachment

    def stop(self) -> None:
        """Wake HTTP observers before the server waits; leave execution running."""
        for attachment in tuple(self._attachments):
            attachment.close()


class Attachment:
    def __init__(
        self,
        source: CanonicalStream,
        store: RunStore,
        after: EventCursor | None,
        admission: StreamReader,
    ) -> None:
        self.source, self.store = source, store
        self.after, self.admitted_after = after, admission.cursor
        self.admission = admission
        self.subscription: EventSubscription | None = None
        self.on_close: Callable[[], None] = lambda: None
        self._closed = False

    def attach(
        self, scope: StreamScope, *, start: Literal["new", "retry"] | None = None
    ) -> EventSubscription:
        """Capture B and the first SQLite read before yielding the admission turn."""
        if self._closed:
            raise RuntimeError("attachment is closed")
        if self.subscription is not None:
            raise RuntimeError("attachment already used")
        # A newly allocated root has no history before this admission. Preserve
        # its first source Begin even when the caller's cursor needs recovery in
        # another scope or epoch. Retry still recovers the existing root from C.
        after = self.admitted_after if start == "new" else self.after
        if after is None and start == "retry":
            after = self.admitted_after
        try:
            with self.source.boundary() as (boundary, floor, cache):
                self.store.pin_stream_snapshot(
                    seconds=SNAPSHOT_SECONDS, max_bytes=SNAPSHOT_BYTES
                )
                reader = self.source.subscribe(
                    after=boundary, root_run_id=scope.root, thread_id=scope.thread
                )
                admission_overflowed = False
                try:
                    self.admission.check()
                except StreamOverflowError:
                    admission_overflowed = True
                self.admission.close()
            self.subscription = EventSubscription(
                reader,
                self.store,
                scope,
                after,
                boundary,
                floor,
                cache,
                started=start == "new" or (start == "retry" and self.after is None),
                admission_overflowed=admission_overflowed,
            )
            return self.subscription
        except BaseException:
            self.store.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.on_close()
        self.admission.close()
        if self.subscription is None:
            self.store.close()
        else:
            self.subscription.close()


class _Snapshot:
    def __init__(
        self, store: RunStore, runs: list[RunRecord], steps: list[StepRecord]
    ) -> None:
        budget = _Budget(time.monotonic() + SNAPSHOT_SECONDS)
        self.runs = {run.id: run for run in runs}
        self.steps = {step.ref: step for step in steps}
        self.begins: dict[str | StepRef, tuple[ExecutionEvent, str | None]] = {}
        self.ends: dict[str | StepRef, tuple[ExecutionEvent, str | None]] = {}
        self.children: dict[str | StepRef | None, list[str | StepRef]] = defaultdict(
            list
        )
        for run in runs:
            control = store.get_run_control(run_id=run.id, index=0)
            runnable = (
                control.payload.runnable
                if control and isinstance(control.payload, RunControlPayload)
                else ""
            )
            self.begins[run.id] = (
                RunBegin(
                    run.id,
                    run.control,
                    runnable,
                    run.parent,
                    run.occur,
                    run.started_at,
                    str(run.thread),
                ),
                run.begin_cursor,
            )
            self.children[run.parent].append(run.id)
            if run.status in _TERMINAL:
                self.ends[run.id] = (
                    RunEnd(
                        run.id,
                        run.status,
                        run.control,
                        store.resolve_output(run.output)
                        if run.output is not None
                        else None,
                        ErrorMessage(store.resolve_error(run.error))
                        if run.error is not None
                        else None,
                        run.finished_at or "",
                    ),
                    run.end_cursor,
                )
            budget.take(event_to_data(self.begins[run.id][0]))
            if run.id in self.ends:
                budget.take(event_to_data(self.ends[run.id][0]))
        for step in steps:
            given = step.given
            if isinstance(given, StoredModelStepGiven):
                given = ModelStepGiven(
                    given.model,
                    store.rebuild_model_call(step),
                    setup=given.setup,
                    state=given.state,
                )
            self.begins[step.ref] = (
                StepBegin(
                    step.ref,
                    step.kind,
                    given,
                    step.state,
                    step.input,
                    step.preceded_by,
                    step.occur,
                    step.started_at,
                ),
                step.begin_cursor,
            )
            self.children[step.parent or step.run_id].append(step.ref)
            if step.status != "running":
                self.ends[step.ref] = (
                    StepEnd(
                        step.ref,
                        step.kind,
                        step.status,
                        store.resolve_output(step.output)
                        if step.output is not None
                        else None,
                        step.noted,
                        ErrorMessage(store.resolve_error(step.error))
                        if step.error is not None
                        else None,
                        step.aborted_by,
                        step.finished_at or "",
                    ),
                    step.end_cursor,
                )

            budget.take(event_to_data(self.begins[step.ref][0]))
            if step.ref in self.ends:
                budget.take(event_to_data(self.ends[step.ref][0]))

    def structural(self) -> Iterator[StreamFrame]:
        # Iterative traversal also bounds Python stack use for deeply nested flows.
        stack = [(key, False) for key in reversed(self.children[None])]
        while stack:
            key, ending = stack.pop()
            if ending:
                if key in self.ends:
                    yield StreamFrame.source(*self.ends[key], context=True)
                continue
            yield StreamFrame.source(*self.begins[key], context=True)
            stack.append((key, True))
            stack.extend((child, False) for child in reversed(self.children[key]))

    def ancestors(self, event: ExecutionEvent) -> list[str | StepRef]:
        key: str | StepRef | None
        if isinstance(event, RunBegin):
            key = event.parent
        elif isinstance(event, RunEnd):
            key = event.run
        elif isinstance(event, StepBegin):
            key = event.step.parent or event.step.run_id
        elif isinstance(event, StepEnd | PartBegin | PartDelta | PartEnd):
            key = event.step
        else:
            return []
        result: list[str | StepRef] = []
        while key is not None:
            result.append(key)
            if isinstance(key, StepRef):
                key = key.parent or key.run_id
            else:
                run = self.runs.get(key)
                if run is None:
                    raise StreamGapError("cached ancestor no longer has records")
                key = run.parent
        return list(reversed(result))


class EventSubscription:
    """One bounded prefix followed by the same canonical suffix for every scope."""

    def __init__(
        self,
        reader: StreamReader,
        store: RunStore,
        scope: StreamScope,
        after: EventCursor | None,
        boundary: EventCursor,
        floor: EventCursor,
        cache: tuple[CanonicalEvent, ...],
        *,
        started: bool,
        admission_overflowed: bool = False,
    ) -> None:
        self.reader, self.store, self.scope = reader, store, scope
        self.after, self.boundary, self.floor, self.cache = (
            after,
            boundary,
            floor,
            cache,
        )
        self.started = started
        self._admission_overflowed = admission_overflowed
        self._prefix: deque[StreamFrame] | None = None
        self._batch: deque[CanonicalEvent] = deque()
        self._checkpoint: EventCursor | None = None
        self._last_sent = after
        self._active: set[str] = set()
        self._suppressed: set[StepRef] = set()
        self._closed = False
        self._deadline = time.monotonic() + SNAPSHOT_SECONDS
        self._expired = False
        self._snapshot_finished = threading.Event()
        self._expiry = asyncio.get_running_loop().call_later(
            SNAPSHOT_SECONDS, self._expire_snapshot
        )
        self._preparing: asyncio.Task[deque[StreamFrame]] | None = None

    def _prepare(self) -> deque[StreamFrame]:
        try:
            same_epoch = (
                self.after is not None
                and self.after.epoch == self.boundary.epoch
                and self.store.stream_has_cursors
            )
            cached = (
                same_epoch
                and self.after is not None
                and self.after.seq >= self.floor.seq
            )
            frames = tuple(
                frame
                for frame in self.cache
                if self.scope.matches(frame)
                and self.after is not None
                and frame.cursor.seq > self.after.seq
            )
            extra = (
                tuple(
                    {
                        frame.root_run_id
                        for frame in frames
                        if frame.root_run_id is not None
                    }
                )
                if cached
                else ()
            )
            runs, steps = self.store.stream_records(
                root=self.scope.root,
                thread=self.scope.thread,
                after=str(self.after) if same_epoch else None,
                complete=self.after is not None and not same_epoch,
                extra_roots=extra,
            )
            self._active = {run.id for run in runs if run.status not in _TERMINAL}
            self._suppressed = {step.ref for step in steps if step.status == "running"}
            self._check_state()
            if self.started and (not cached or self._admission_overflowed):
                raise StreamOverflowError("admission outgrew the canonical cache")
            retry_start = self.started and any(
                isinstance(frame.event, RunRetried) for frame in frames
            )
            if self.started and not retry_start:
                prefix = _Frames(self._deadline)
                prefix.extend(
                    StreamFrame.source(frame.event, str(frame.cursor))
                    for frame in frames
                )
                self._suppressed.clear()
            else:
                snapshot = _Snapshot(self.store, runs, steps)
                try:
                    if not cached or any(
                        isinstance(frame.event, RunRetried) for frame in frames
                    ):
                        raise StreamGapError("records replacement required")
                    prefix = self._replay(snapshot, frames)
                except StreamGapError:
                    self._suppressed = {
                        step.ref for step in steps if step.status == "running"
                    }
                    prefix = _Frames(self._deadline)
                    if retry_start:
                        prefix.extend(
                            StreamFrame.source(frame.event, str(frame.cursor))
                            for frame in frames
                            if isinstance(frame.event, RunRetried)
                        )
                    prefix.append(
                        StreamFrame(
                            "stream_prefill",
                            {
                                "cursor": str(self.boundary),
                                "scope": self.scope.data(),
                                "roots": None
                                if self.after is not None and not same_epoch
                                else [run.id for run in runs if run.parent is None],
                            },
                        )
                    )
                    if self.scope.root is None:
                        for control in self.store.stream_thread_controls(
                            thread=self.scope.thread,
                            after=str(self.after) if same_epoch else None,
                        ):
                            payload = control.payload
                            event: ExecutionEvent
                            thread = str(control.target)
                            if isinstance(payload, CreateControlPayload):
                                record = self.store.get_thread(thread_id=thread)
                                if record is None:
                                    continue
                                event = ThreadCreated(
                                    thread,
                                    control.ref,
                                    record.origin,
                                    record.peer,
                                    record.created_at,
                                )
                            elif isinstance(payload, ForkControlPayload):
                                event = ThreadForked(
                                    thread,
                                    control.ref,
                                    str(payload.fork_from),
                                    str(payload.fork_at),
                                    control.created_at,
                                )
                            elif isinstance(payload, RewindControlPayload):
                                event = ThreadRewound(
                                    thread,
                                    control.ref,
                                    str(payload.rewind_from),
                                    self.store.stream_rewound_runs(payload),
                                    control.created_at,
                                )
                            else:
                                continue
                            prefix.append(
                                StreamFrame.source(
                                    event, control.event_cursor, context=True
                                )
                            )
                    prefix.extend(snapshot.structural())
                prefix.append(StreamFrame.checkpoint(self.boundary))
            return deque(prefix)
        except (sqlite3.Error, ValueError) as exc:
            raise SnapshotLimitError(str(exc)) from exc
        finally:
            self.cache = ()
            self.store.close()
            self._snapshot_finished.set()

    def _expire_snapshot(self) -> None:
        if self._snapshot_finished.is_set():
            return
        self._expired = True
        self.store.expire_stream_snapshot()
        self.reader.close()
        if self._preparing is None:
            self.store.close()
            self._snapshot_finished.set()

    def _replay(
        self, snapshot: _Snapshot, frames: tuple[CanonicalEvent, ...]
    ) -> _Frames:
        prefix = _Frames(self._deadline)
        seen: set[str | StepRef] = set()
        for frame in frames:
            event = frame.event
            if isinstance(event, _PARTS) and event.step in snapshot.ends:
                continue
            for key in snapshot.ancestors(event):
                if key in seen:
                    continue
                begin, cursor = snapshot.begins[key]
                if (
                    cursor is None
                    or EventCursor.parse(cursor).epoch != frame.cursor.epoch
                    or EventCursor.parse(cursor).seq > frame.cursor.seq
                ):
                    raise StreamGapError(
                        "cached ancestor belongs to another incarnation"
                    )
                prefix.append(StreamFrame.source(begin, cursor, context=True))
                seen.add(key)
            if isinstance(event, StepBegin):
                seen.add(event.step)
                self._suppressed.discard(event.step)
            elif isinstance(event, RunBegin):
                seen.add(event.run)
            if isinstance(event, _PARTS) and event.step in self._suppressed:
                continue
            prefix.append(StreamFrame.source(event, str(frame.cursor)))
        # An idle attachment still needs its active ancestors before future Ends.
        for key in [*self._active, *self._suppressed]:
            begin, _cursor = snapshot.begins[key]
            for ancestor in [*snapshot.ancestors(begin), key]:
                if ancestor not in seen:
                    prefix.append(
                        StreamFrame.source(*snapshot.begins[ancestor], context=True)
                    )
                    seen.add(ancestor)
        return prefix

    def _check_state(self) -> None:
        if len(self._active) + len(self._suppressed) > OPEN_ENTITIES:
            raise StreamOverflowError("subscription open-entity budget exceeded")

    async def receive(self) -> StreamFrame:
        if self._expired:
            raise SnapshotLimitError("stream snapshot expired")
        if self._closed:
            raise StopAsyncIteration
        if self._prefix is None:
            if self._preparing is None:
                self._preparing = asyncio.create_task(asyncio.to_thread(self._prepare))
            # Keepalive cancellation must not strand a snapshot worker or restart it.
            try:
                self._prefix = await asyncio.wait_for(
                    asyncio.shield(self._preparing),
                    max(0, self._deadline - time.monotonic()),
                )
            except TimeoutError as exc:
                self._expire_snapshot()
                raise SnapshotLimitError("stream snapshot expired") from exc
            finally:
                if self._snapshot_finished.is_set():
                    self._expiry.cancel()
        if self._expired:
            raise SnapshotLimitError("stream snapshot expired")
        if self._closed:
            raise StopAsyncIteration
        self.reader.check()
        if self._prefix:
            frame = self._prefix.popleft()
            if frame.id is not None:
                self._last_sent = EventCursor.parse(frame.id)
            return frame
        while True:
            if not self._batch:
                if (
                    self.scope.root is not None
                    and not self._active
                    and self.reader.empty
                ):
                    raise StopAsyncIteration
                batch = await self.reader.receive()
                self._batch.extend(batch.events)
                self._checkpoint = batch.cursor
                if not self._batch:
                    continue
            frame = self._batch.popleft()
            event = frame.event
            if isinstance(event, RunBegin):
                self._active.add(event.run)
            elif isinstance(event, RunEnd):
                self._active.discard(event.run)
            elif isinstance(event, RunRetried):
                self._active.difference_update(event.removed_runs)
                self._active.add(event.run)
                self._suppressed.difference_update(event.invalidated_steps)
            elif isinstance(event, StepBegin | StepEnd):
                self._suppressed.discard(event.step)
            self._check_state()
            if isinstance(event, _PARTS) and event.step in self._suppressed:
                continue
            self._last_sent = frame.cursor
            return StreamFrame.source(event, str(frame.cursor))

    def checkpoint(self) -> StreamFrame | None:
        """Advance over filtered events only after every acquired frame was sent."""
        if (
            self._prefix is None
            or self._prefix
            or self._batch
            or self._checkpoint is None
        ):
            return None
        if self._last_sent is None or self._checkpoint.seq > self._last_sent.seq:
            self._last_sent = self._checkpoint
            return StreamFrame.checkpoint(self._checkpoint)
        return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._expiry.cancel()
        if not self._snapshot_finished.is_set():
            self.store.expire_stream_snapshot()
        self.cache = ()
        self.reader.close()
        self._batch.clear()
        if self._prefix is not None:
            self._prefix.clear()
        if self._preparing is None:
            self.cache = ()
            self.store.close()
        else:
            # The worker closes its own connection. Never block network cleanup
            # on reconstruction, and retrieve exceptions after a disconnect.
            self._preparing.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )
