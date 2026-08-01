"""Terminal-independent state derived from ordered execution events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Self

from toolang.execution.events import RunBegin, RunEnd, StepBegin, StepEnd

from .formatting import (
    active_step_label,
    completed_step_label,
    count,
    integer,
    mapping,
    runnable_label,
    statement_head,
    statement_target,
    text,
    token_fact,
)


@dataclass(slots=True)
class Metrics:
    """Aggregate work performed by one run tree or statement."""

    runs: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: Metrics) -> None:
        self.runs += other.runs
        self.model_calls += other.model_calls
        self.tool_calls += other.tool_calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens

    def record_step(self, event: StepEnd) -> None:
        if event.kind == "model":
            self.model_calls += 1
            usage = mapping(event.noted.get("usage"))
            self.input_tokens += integer(usage.get("input_tokens")) or 0
            self.output_tokens += integer(usage.get("output_tokens")) or 0
        elif event.kind == "tool":
            self.tool_calls += 1

    def facts(
        self,
        *,
        duration: str = "",
        include_runs: bool = True,
    ) -> list[str]:
        facts = [duration]
        if include_runs and self.runs:
            facts.append(count(self.runs, "run"))
        if self.input_tokens or self.output_tokens:
            facts.append(token_fact(self.input_tokens, self.output_tokens))
        if self.model_calls:
            facts.append(count(self.model_calls, "model call"))
        if self.tool_calls:
            facts.append(count(self.tool_calls, "tool call"))
        return [fact for fact in facts if fact]


@dataclass(slots=True)
class RunState:
    """Semantic state for one root or recursive run."""

    run_id: str
    parent: str | None
    kind: str
    name: str
    placement: Mapping[str, object]
    started_at: str
    metrics: Metrics = field(default_factory=lambda: Metrics(runs=1))
    status: str = "running"
    finished_at: str = ""

    @classmethod
    def from_event(cls, event: RunBegin) -> Self:
        runnable = mapping(event.context.get("runnable"))
        return cls(
            run_id=event.run,
            parent=event.parent,
            kind=text(runnable.get("kind")) or "run",
            name=runnable_label(text(runnable.get("name"))),
            placement=mapping(event.context.get("placement")),
            started_at=event.started_at,
        )

    @property
    def label(self) -> str:
        return " ".join(value for value in (self.kind, self.name) if value)

    def finish(self, event: RunEnd) -> None:
        self.status = event.status
        self.finished_at = event.finished_at


@dataclass(slots=True)
class LaneState:
    """Current work assigned to one bounded parallel presentation lane."""

    run_id: str
    item: int
    activity: str = "starting…"


@dataclass(slots=True)
class CallState:
    """Semantic state for one atomic model, tool, or system step."""

    begin: StepBegin
    preview: str = ""
    end: StepEnd | None = None

    @property
    def active_label(self) -> str:
        return active_step_label(self.begin)

    def completed_label(self, event: StepEnd) -> str:
        return completed_step_label(self.begin, event)

    def append_delta(self, delta: str) -> str:
        self.preview = (self.preview + delta)[-800:]
        return self.preview

    def finish(self, event: StepEnd) -> None:
        self.end = event


@dataclass(slots=True)
class StatementState:
    """Semantic state for one authored flow statement and its child work."""

    begin: StepBegin
    children: list[str] = field(default_factory=list)
    completed: int = 0
    failed: int = 0
    failed_item: int | None = None
    total: int | None = None
    lane_count: int | None = None
    lanes: dict[int, LaneState] = field(default_factory=dict)
    metrics: Metrics = field(default_factory=Metrics)
    active_run: str | None = None
    active_item: int | None = None
    active_activity: str = "starting…"
    live_owner: str | None = None
    ordinal: int | None = None
    current_iteration: int | None = None
    next_ordinal: int = 0
    active_ordinal: int | None = None
    active_title: str = ""
    active_work: str = ""
    until_decision: bool | None = None
    end: StepEnd | None = None

    @property
    def statement(self) -> str:
        return text(self.begin.given.get("statement"))

    @property
    def batched(self) -> bool:
        return self.begin.kind in {"par", "loop"}

    def child_started(self, run: RunState) -> None:
        self.children.append(run.run_id)
        self.active_run = run.run_id
        item = integer(run.placement.get("item"))
        self.active_item = item
        self.active_activity = "starting…"
        if (total := integer(run.placement.get("items"))) is not None:
            self.total = max(self.total or 0, total)
        lane = integer(run.placement.get("lane"))
        lanes = integer(run.placement.get("lanes"))
        if lane is not None and item is not None:
            self.lanes[lane] = LaneState(run.run_id, item)
        if lanes is not None:
            self.lane_count = max(self.lane_count or 0, lanes)

    def note_iteration(self, iteration: int) -> int:
        """Enter one repeat iteration and allocate its local ordinal."""

        if self.current_iteration != iteration:
            self.current_iteration = iteration
            self.next_ordinal = 0
        ordinal = self.next_ordinal
        self.next_ordinal += 1
        return ordinal

    def activate_nested(self, block: StatementState) -> None:
        self.active_ordinal = block.ordinal
        self.active_title = statement_head(block.begin.given)
        self.active_work = ""
        self.active_activity = ""

    def begin_until(self, run: RunState) -> None:
        iteration = integer(run.placement.get("loop"))
        if iteration is not None:
            self.current_iteration = iteration
        self.active_ordinal = None
        self.active_title = "until"
        self.active_work = f"Run {run.label}"
        self.active_activity = "starting…"

    def record_until_decision(self, decision: bool | None) -> None:
        self.until_decision = decision
        if decision is not None:
            self.active_activity = f"↳ {'stop repeating' if decision else 'continue'}"

    def child_finished(self, run: RunState) -> None:
        self.metrics.add(run.metrics)
        if run.status == "finished":
            self.completed += 1
        elif run.status == "failed":
            self.failed += 1
            self.failed_item = integer(run.placement.get("item"))
        lane = integer(run.placement.get("lane"))
        if (
            lane is not None
            and (current := self.lanes.get(lane)) is not None
            and current.run_id == run.run_id
        ):
            self.lanes.pop(lane, None)
        if self.active_run == run.run_id:
            self.active_run = None
            self.active_item = None

    def work_line(self, run: RunState | None = None) -> str:
        label = run.label if run is not None else self._unresolved_run_label()
        total = self.total or len(self.children)
        lanes = self.lane_count or integer(self.begin.given.get("par"))
        if self.begin.kind == "par":
            unit = "times" if self.statement == "storm" else "items"
            details = count(total, "item") if unit == "items" else f"{total} times"
            if lanes is not None:
                details = f"{details}, {count(lanes, 'lane')}"
            return f"Run {label} in parallel ({details})"
        if self.statement == "settle":
            return f"Run {label} sequentially ({count(total, 'item')})"
        return f"Run {label}"

    def _unresolved_run_label(self) -> str:
        target = statement_target(self.begin.given)
        return f"agic {target}" if target else "agic"

    def set_activity(self, run_id: str, activity: str) -> None:
        if self.active_run == run_id:
            self.active_activity = activity
        for lane in self.lanes.values():
            if lane.run_id == run_id:
                lane.activity = activity

    def finish(self, event: StepEnd) -> None:
        self.end = event
