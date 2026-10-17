"""Client-side cursor commits and structural replacement before presentation."""

from __future__ import annotations


from collections import defaultdict
import json
from typing import Any

from .events import (
    ExecutionEvent,
    PartBegin,
    PartDelta,
    PartEnd,
    RunBegin,
    RunEnd,
    RunEvent,
    RunSnapshot,
    StepBegin,
    StepEnd,
    event_from_data,
)
from .schemas import STREAM_PREFILL_MAX_BYTES, StreamFrame
from .types import EventCursor, StepRef


class StreamClientState:
    """Bounded run-tree reducer shared by HTTP consumers.

    Checkpoints commit only a complete replacement. Structural context is an
    upsert: repeating a Begin cannot clear a completed incarnation.
    """

    def __init__(self) -> None:
        self.cursor: str | None = None
        self._records: dict[tuple[str, str], ExecutionEvent] = {}
        self._positions: dict[tuple[str, str], str | None] = {}
        self._prefix: list[StreamFrame] | None = None
        self._replacement: dict[str, Any] | None = None
        self._bytes = 0

    def copy(self) -> StreamClientState:
        """Copy committed structural state without an in-flight prefix."""
        result = StreamClientState()
        result.cursor = self.cursor
        result._records = dict(self._records)
        result._positions = dict(self._positions)
        return result

    def forget(self, roots: set[str]) -> None:
        """Release whole trees while retaining the last transport checkpoint."""
        self._remove_runs(self._descendants(roots))

    def attach(self) -> RunSnapshot:
        self._prefix = None
        self._replacement = None
        self._bytes = 0
        return self.snapshot()

    def feed(self, frame: StreamFrame) -> tuple[ExecutionEvent | RunSnapshot, ...]:
        if frame.event == "stream_prefill":
            if self._prefix is not None:
                raise ValueError("nested or acknowledged stream prefill")
            EventCursor.parse(frame.data["cursor"])
            scope = frame.data.get("scope")
            roots = frame.data.get("roots")
            if (
                not isinstance(scope, dict)
                or scope.get("kind") not in {"run", "thread", "agent"}
                or roots is not None
                and (
                    not isinstance(roots, list)
                    or not all(isinstance(root, str) for root in roots)
                )
            ):
                raise ValueError("invalid stream replacement scope")
            self._replacement = frame.data
            self._prefix = []
            return ()
        if frame.event == "stream_checkpoint":
            cursor = str(EventCursor.parse(frame.data["cursor"]))
            if frame.id != cursor:
                raise ValueError("checkpoint ID does not match its boundary")
            if self._prefix is not None:
                replacement = self._replacement
                if replacement is None or cursor != replacement["cursor"]:
                    raise ValueError("checkpoint does not complete its prefill")
                candidate = self.copy()
                candidate._replace(replacement)
                mutations: list[ExecutionEvent] = []
                for item in self._prefix:
                    event = event_from_data(item.data)
                    candidate._apply(event, item.data.get("cursor"))
                    if not isinstance(event, RunBegin | RunEnd | StepBegin | StepEnd):
                        mutations.append(event)
                self._records = candidate._records
                self._positions = candidate._positions
                self._prefix = None
                self._replacement = None
                self._bytes = 0
                self.cursor = cursor
                return (*mutations, self.snapshot())
            if self.cursor is not None:
                previous = EventCursor.parse(self.cursor)
                next_cursor = EventCursor.parse(cursor)
                if (
                    previous.epoch == next_cursor.epoch
                    and next_cursor.seq < previous.seq
                ):
                    raise ValueError("checkpoint moved backwards")
            self.cursor = cursor
            return ()
        if frame.data.get("context") is True:
            frame = StreamFrame(frame.event, frame.data)
        event = event_from_data(frame.data)
        if event.type != frame.event:
            raise ValueError("event name does not match its payload")
        if self._prefix is not None:
            if frame.id is not None or isinstance(
                event, PartBegin | PartDelta | PartEnd
            ):
                raise ValueError(
                    "prefill must contain unacknowledged structural events"
                )
            self._bytes += len(json.dumps(frame.data, ensure_ascii=False).encode())
            if self._bytes > STREAM_PREFILL_MAX_BYTES or len(self._prefix) >= 20000:
                raise ValueError("stream prefill exceeds client budget")
            self._prefix.append(frame)
            return ()
        if frame.id is not None:
            cursor = EventCursor.parse(frame.id)
            if self.cursor is not None:
                previous = EventCursor.parse(self.cursor)
                if cursor.epoch == previous.epoch and cursor.seq <= previous.seq:
                    return ()
            self.cursor = str(cursor)
        replacing = (
            isinstance(event, RunBegin | StepBegin)
            and (
                event.type,
                event.run
                if isinstance(event, RunBegin)
                else str(StepRef.parse(event.step)),
            )
            in self._records
        )
        changed = self._apply(event, frame.data.get("cursor") or frame.id)
        if changed and replacing:
            return (self.snapshot(),)
        return (event,) if changed else ()

    def _replace(self, replacement: dict[str, Any]) -> None:
        roots = replacement["roots"]
        scope = replacement["scope"]
        selected = (
            set(roots)
            if roots is not None
            else {
                event.run
                for event in self._records.values()
                if isinstance(event, RunBegin)
                and (event.parent) is None
                and (
                    scope["kind"] == "agent"
                    or scope["kind"] == "run"
                    and event.run == scope.get("id")
                    or scope["kind"] == "thread"
                    and event.thread_id == scope.get("id")
                )
            }
        )
        self._remove_runs(self._descendants(selected))

    def _descendants(self, runs: set[str]) -> set[str]:
        selected = set(runs)
        for event in self._records.values():
            if (
                isinstance(event, RunBegin)
                and (event.parent) is not None
                and (StepRef.parse(event.parent)).run_id in selected
            ):
                selected.add(event.run)
        return selected

    def _remove_runs(self, runs: set[str]) -> None:
        for key, event in tuple(self._records.items()):
            run = (
                event.run
                if isinstance(event, RunBegin | RunEnd)
                else StepRef.parse(event.step).run_id
                if isinstance(event, StepBegin | StepEnd)
                else None
            )
            if run in runs:
                self._records.pop(key)
                self._positions.pop(key, None)

    def _apply(self, event: ExecutionEvent, cursor: str | None) -> bool:
        if not isinstance(event, RunBegin | RunEnd | StepBegin | StepEnd):
            return True
        ref = (
            event.run
            if isinstance(event, RunBegin | RunEnd)
            else str(StepRef.parse(event.step))
        )
        key = (event.type, ref)
        previous = self._records.get(key)
        old_cursor = self._positions.get(key)
        if cursor is not None and old_cursor is not None:
            incoming, applied = EventCursor.parse(cursor), EventCursor.parse(old_cursor)
            if incoming.epoch == applied.epoch and incoming.seq < applied.seq:
                return False
        if previous is not None and (
            previous == event
            or cursor is not None
            and self._positions.get(key) == cursor
        ):
            return False
        if isinstance(event, RunEnd) and any(
            isinstance(begin, RunBegin)
            and begin.parent is not None
            and StepRef.parse(begin.parent).run_id == event.run
            and ("run_end", begin.run) not in self._records
            for begin in self._records.values()
        ):
            raise ValueError(f"RunEnd with active child Run for {event.run}")
        if isinstance(event, RunBegin | StepBegin):
            end = ("run_end" if isinstance(event, RunBegin) else "step_end", ref)
            if (
                previous is not None
                and cursor is not None
                and cursor != self._positions.get(key)
            ):
                self._records.pop(end, None)
                self._positions.pop(end, None)
        self._records[key] = event
        self._positions[key] = cursor
        if len(self._records) > 20000:
            raise ValueError("stream client structural budget exceeded")
        return True

    def snapshot(self) -> RunSnapshot:
        children: dict[str | StepRef | None, list[str | StepRef]] = defaultdict(list)
        begins: dict[str | StepRef, RunBegin | StepBegin] = {}
        ends: dict[str | StepRef, RunEnd | StepEnd] = {}
        for event in self._records.values():
            if isinstance(event, RunBegin):
                begins[event.run] = event
                children[
                    (StepRef.parse(event.parent) if event.parent is not None else None)
                ].append(event.run)
            elif isinstance(event, StepBegin):
                begins[StepRef.parse(event.step)] = event
                children[
                    StepRef.parse(event.step).parent or StepRef.parse(event.step).run_id
                ].append(StepRef.parse(event.step))
            elif isinstance(event, RunEnd):
                ends[event.run] = event
            elif isinstance(event, StepEnd):
                ends[StepRef.parse(event.step)] = event
        events: list[RunEvent] = []
        stack = [(key, False) for key in reversed(children[None])]
        while stack:
            key, ending = stack.pop()
            if ending:
                if key in ends:
                    events.append(ends[key])
            else:
                events.append(begins[key])
                stack.append((key, True))
                stack.extend((child, False) for child in reversed(children[key]))
        return RunSnapshot(tuple(events))

    def complete(self, root: str) -> bool:
        return self._prefix is None and ("run_end", root) in self._records
