"""Shared structural normalization for local and backend observation."""

from __future__ import annotations
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
import json
import time
from typing import Any, cast
from toolang.lang.ast import AwaitStmt
from .errors import StreamGapError, StreamOverflowError, SnapshotLimitError
from .events import (
    ExecutionEvent,
    StepInfo,
    FlowStepInfo,
    FlowOperation,
    ModelStepInfo,
    ToolStepInfo,
    StepSummary,
    Usage,
    EventOutput,
    ControlApplied,
    PartBegin,
    PartDelta,
    PartEnd,
    RunBegin,
    RunEnd,
    StepBegin,
    StepEnd,
    ThreadForked,
)
from .schemas import STREAM_PREFILL_MAX_BYTES, StreamFrame
from .stream import CanonicalEvent
from .types import (
    EventCursor,
    StepRef,
    StepGiven,
    StepNoted,
    Output,
    TypedRef,
    ErrorRef,
    ModelStepGiven,
    ToolStepGiven,
    ModelStepNoted,
    ToolStepNoted,
    ExecStepNoted,
    CollectionStepNoted,
)
from .records import StoredStepGiven, StoredModelStepGiven, StepRecord
from .accounting import (
    token_meter_quantity,
    selected_usd_cost,
    selected_cost_is_approximate,
)

SNAPSHOT_SECONDS = 5.0
SNAPSHOT_BYTES = STREAM_PREFILL_MAX_BYTES
OPEN_ENTITIES = 4096
_PARTS = (PartBegin, PartDelta, PartEnd)


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
            key = StepRef.parse(event.parent) if event.parent is not None else None
        elif isinstance(event, RunEnd):
            key = event.run
        elif isinstance(event, StepBegin):
            key = StepRef.parse(event.step).parent or StepRef.parse(event.step).run_id
        elif isinstance(event, StepEnd | PartBegin | PartDelta | PartEnd):
            key = StepRef.parse(event.step)
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
        elif isinstance(event, StepBegin | StepEnd):
            self._suppressed.discard(StepRef.parse(event.step))
            if isinstance(event, StepBegin):
                self._open_steps.add(StepRef.parse(event.step))
            else:
                self._open_steps.discard(StepRef.parse(event.step))
        self._check_state()
        return not (
            isinstance(event, _PARTS) and StepRef.parse(event.step) in self._suppressed
        )

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
            if isinstance(event, _PARTS) and StepRef.parse(event.step) in snapshot.ends:
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
                seen.add(StepRef.parse(event.step))
                self._suppressed.discard(StepRef.parse(event.step))
            elif isinstance(event, RunBegin):
                seen.add(event.run)
            if (
                isinstance(event, _PARTS)
                and StepRef.parse(event.step) in self._suppressed
            ):
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


def step_info(given: StepGiven | StoredStepGiven) -> StepInfo:
    """Select presentation facts without assembling a model call or traversing AST bodies."""
    if isinstance(given, ModelStepGiven | StoredModelStepGiven):
        return ModelStepInfo(given.model)
    if isinstance(given, ToolStepGiven):
        name = given.call.name
        runtime = given.plugin in {"runtime", "_toolang"}
        awaiting = runtime and name in {"runtime__await", "_toolang__await"}
        launching = (
            name
            in {
                "runtime__run",
                "_toolang__run",
                "runtime__exec",
                "_toolang__exec",
                "_toolang__execute",
            }
            or runtime
            and name in {"runtime__spawn", "_toolang__spawn", "spawn"}
        )
        target = (
            given.call.input.get("target" if awaiting else "runnable", "")
            if awaiting or launching
            else ""
        )
        return ToolStepInfo(
            given.plugin,
            given.call.name,
            given.summary,
            target if isinstance(target, str) else "",
            name in {"runtime__run", "_toolang__run"}
            and given.call.input.get("async") is True,
        )
    return FlowStepInfo(
        kind=cast(FlowOperation, given.kind),
        runnable=getattr(given, "runnable", None),
        handle=getattr(given, "handle", ""),
        name=getattr(given, "name", "") or "",
        binding=given.binding,
        doc=given.doc,
        asynchronous=getattr(given, "asynchronous", False),
        count=getattr(given, "count", None),
        lanes=getattr(given, "lanes", None),
        position=getattr(given, "position", None),
        order=getattr(given, "order", "ascending"),
    )


def step_summary(noted: StepNoted) -> StepSummary | None:
    if noted is None:
        return None
    if isinstance(noted, ModelStepNoted):
        accounting = noted.accounting
        if accounting is None:
            return None
        return StepSummary(
            usage=Usage(
                accounting.input_tokens,
                accounting.output_tokens,
                token_meter_quantity(accounting, "input.cache_read"),
                token_meter_quantity(accounting, "output.reasoning"),
                selected_usd_cost(accounting),
                selected_cost_is_approximate(accounting),
            )
        )
    if isinstance(noted, ToolStepNoted):
        return StepSummary(summary=noted.summary)
    if isinstance(noted, ExecStepNoted):
        return StepSummary(runnable=noted.runnable)
    if isinstance(noted, CollectionStepNoted):
        return StepSummary(
            total_items=noted.total_items, output_items=noted.output_items
        )
    return StepSummary(
        iterations=noted.iterations, termination=noted.termination, total=noted.total
    )


def direct_output(output: Output | None) -> EventOutput | None:
    if output is None:
        return None
    # A durable output edge alone does not ask clients to materialize a value.
    if isinstance(output.value, TypedRef):
        return None
    return EventOutput(output.type, output.value)


def step_begin_event(step: StepRecord) -> StepBegin:
    return StepBegin(
        step=step.id,
        run=step.run_id,
        parent=str(step.parent) if step.parent else None,
        index=step.index,
        kind=step.kind,
        given=step_info(step.given),
        preceded_by=tuple(
            ControlApplied(str(ref.target), ref.index) for ref in step.preceded_by
        ),
        occurrence=step.occur,
        started_at=step.started_at,
    )


def step_end_event(
    step: StepRecord, output: EventOutput | None, error: str | None
) -> StepEnd:
    return StepEnd(
        step=step.id,
        run=step.run_id,
        kind=step.kind,
        status=step.status,
        output=output,
        noted=step_summary(step.noted),
        error_source=str(step.error.ref.record)
        if isinstance(step.error, ErrorRef)
        and not (
            isinstance(step.given, AwaitStmt)
            or isinstance(step.given, ToolStepGiven)
            and step.given.call.name in {"runtime__await", "_toolang__await"}
        )
        else None,
        error=error,
        finished_at=step.finished_at or "",
    )
