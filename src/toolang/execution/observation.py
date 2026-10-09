"""Shared structural normalization for local and backend observation."""

from __future__ import annotations
from collections import defaultdict
from collections.abc import Iterable, Iterator
import json
import time
from typing import Any
from .errors import StreamGapError, StreamOverflowError, SnapshotLimitError
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
)
from .schemas import STREAM_PREFILL_MAX_BYTES, StreamFrame
from .stream import CanonicalEvent
from .types import EventCursor, StepRef

SNAPSHOT_SECONDS = 5.0
SNAPSHOT_BYTES = STREAM_PREFILL_MAX_BYTES
OPEN_ENTITIES = 4096
_PARTS = (PartBegin, PartDelta, PartEnd)


class SnapshotBudget:
    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.bytes = 0

    def take(self, data: dict[str, Any]) -> None:
        self.bytes += len(json.dumps(data, ensure_ascii=False).encode())
        if self.bytes > SNAPSHOT_BYTES or time.monotonic() > self.deadline:
            raise SnapshotLimitError("stream snapshot budget exceeded")


class SnapshotFrames(list[StreamFrame]):
    """Check serialized size while building, not after allocating the prefix."""

    def __init__(self, deadline: float) -> None:
        super().__init__()
        self.budget = SnapshotBudget(deadline)

    def append(self, frame: StreamFrame) -> None:
        self.budget.take(frame.data)
        super().append(frame)

    def extend(self, frames: Iterable[StreamFrame]) -> None:
        for frame in frames:
            self.append(frame)


class StructuralSnapshot:
    def __init__(self) -> None:
        self.parents: dict[str, StepRef | None] = {}
        self.begins: dict[str | StepRef, tuple[ExecutionEvent, str | None]] = {}
        self.ends: dict[str | StepRef, tuple[ExecutionEvent, str | None]] = {}
        self.children: dict[str | StepRef | None, list[str | StepRef]] = defaultdict(
            list
        )

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
                run = self.parents.get(key)
                if key not in self.parents:
                    raise StreamGapError("cached ancestor no longer has records")
                key = run
        return list(reversed(result))


class StreamNormalizer:
    def __init__(self) -> None:
        self._active: set[str] = set()
        self._suppressed: set[StepRef] = set()
        self._open_steps: set[StepRef] = set()

    def seed(self, snapshot: StructuralSnapshot) -> None:
        self._active = {
            key
            for key in snapshot.begins
            if isinstance(key, str) and key not in snapshot.ends
        }
        self._suppressed = {
            key
            for key in snapshot.begins
            if isinstance(key, StepRef) and key not in snapshot.ends
        }
        self._open_steps = set(self._suppressed)
        self._check_state()

    @property
    def open_entities(self) -> int:
        return len(self._active) + len(self._open_steps)

    @property
    def active(self) -> bool:
        return bool(self._active)

    def advance(self, event: ExecutionEvent) -> bool:
        if isinstance(event, RunBegin):
            self._active.add(event.run)
        elif isinstance(event, RunEnd):
            self._active.discard(event.run)
        elif isinstance(event, RunRetried):
            self._active.difference_update(event.removed_runs)
            self._active.add(event.run)
            self._suppressed.difference_update(event.invalidated_steps)
            self._open_steps.difference_update(event.invalidated_steps)
            self._open_steps = {
                step
                for step in self._open_steps
                if step.run_id not in event.removed_runs
            }
        elif isinstance(event, StepBegin | StepEnd):
            self._suppressed.discard(event.step)
            if isinstance(event, StepBegin):
                self._open_steps.add(event.step)
            else:
                self._open_steps.discard(event.step)
        self._check_state()
        return not (isinstance(event, _PARTS) and event.step in self._suppressed)

    def replay(
        self,
        snapshot: StructuralSnapshot,
        frames: tuple[CanonicalEvent, ...],
        *,
        deadline: float,
    ) -> SnapshotFrames:
        prefix = SnapshotFrames(deadline)
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
        if self.open_entities > OPEN_ENTITIES:
            raise StreamOverflowError("subscription open-entity budget exceeded")
