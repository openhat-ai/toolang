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
    RunRetried,
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
                self._replace(replacement)
                mutations: list[ExecutionEvent] = []
                for item in self._prefix:
                    event = event_from_data(item.data)
                    self._apply(event, item.data.get("cursor"))
                    if not isinstance(event, RunBegin | RunEnd | StepBegin | StepEnd):
                        mutations.append(event)
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
        if isinstance(event, RunRetried):
            begin = self._records.get(("run_begin", event.run))
            if (
                isinstance(begin, RunBegin)
                and begin.control.index >= event.control.index
            ):
                return ()
            self._invalidate(event)
            return (event, self.snapshot())
        replacing = (
            isinstance(event, RunBegin | StepBegin)
            and (
                event.type,
                event.run if isinstance(event, RunBegin) else str(event.step),
            )
            in self._records
        )
        changed = self._apply(event, frame.data.get("cursor") or frame.id)
        if changed and replacing:
            return (self.snapshot(),)
        if changed and isinstance(event, RunEnd):
            incomplete = self._incomplete_runs()
            if event.run in incomplete:
                return ()
            begin = self._records.get(("run_begin", event.run))
            if (
                isinstance(begin, RunBegin)
                and begin.parent is not None
                and ("run_end", begin.parent.run_id) in self._records
                and begin.parent.run_id not in incomplete
            ):
                # Release deferred ancestor Ends in structural order, including
                # intermediate background runs while other branches stay active.
                return (self.snapshot(),)
        return (event,) if changed else ()

    def _incomplete_runs(self) -> set[str]:
        parents = {
            event.run: event.parent.run_id if event.parent is not None else None
            for event in self._records.values()
            if isinstance(event, RunBegin)
        }
        incomplete: set[str] = set()
        for run in parents:
            if ("run_end", run) in self._records:
                continue
            current: str | None = run
            while current is not None and current not in incomplete:
                incomplete.add(current)
                current = parents.get(current)
        return incomplete

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
                and event.parent is None
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
                and event.parent is not None
                and event.parent.run_id in selected
            ):
                selected.add(event.run)
        return selected

    def _remove_runs(self, runs: set[str]) -> None:
        for key, event in tuple(self._records.items()):
            run = (
                event.run
                if isinstance(event, RunBegin | RunEnd)
                else event.step.run_id
                if isinstance(event, StepBegin | StepEnd)
                else None
            )
            if run in runs:
                self._records.pop(key)
                self._positions.pop(key, None)

    def _invalidate(self, event: RunRetried) -> None:
        self._remove_runs(set(event.removed_runs))
        for ref in event.invalidated_steps:
            for kind in ("step_begin", "step_end"):
                self._records.pop((kind, str(ref)), None)
                self._positions.pop((kind, str(ref)), None)
        self._records.pop(("run_end", event.run), None)
        self._positions.pop(("run_end", event.run), None)

    def _apply(self, event: ExecutionEvent, cursor: str | None) -> bool:
        if not isinstance(event, RunBegin | RunEnd | StepBegin | StepEnd):
            return True
        ref = event.run if isinstance(event, RunBegin | RunEnd) else str(event.step)
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
        incomplete = self._incomplete_runs()
        children: dict[str | StepRef | None, list[str | StepRef]] = defaultdict(list)
        begins: dict[str | StepRef, RunBegin | StepBegin] = {}
        ends: dict[str | StepRef, RunEnd | StepEnd] = {}
        for event in self._records.values():
            if isinstance(event, RunBegin):
                begins[event.run] = event
                children[event.parent].append(event.run)
            elif isinstance(event, StepBegin):
                begins[event.step] = event
                children[event.step.parent or event.step.run_id].append(event.step)
            elif isinstance(event, RunEnd):
                ends[event.run] = event
            elif isinstance(event, StepEnd):
                ends[event.step] = event
        events: list[RunEvent] = []
        stack = [(key, False) for key in reversed(children[None])]
        while stack:
            key, ending = stack.pop()
            if ending:
                if key in ends:
                    begin = begins[key]
                    if isinstance(begin, RunBegin) and begin.run in incomplete:
                        continue
                    events.append(ends[key])
            else:
                events.append(begins[key])
                stack.append((key, True))
                stack.extend((child, False) for child in reversed(children[key]))
        return RunSnapshot(tuple(events))

    def complete(self, root: str) -> bool:
        return (
            self._prefix is None
            and ("run_end", root) in self._records
            and root not in self._incomplete_runs()
        )
