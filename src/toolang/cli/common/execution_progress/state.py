"""Typed active state for execution progress projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from toolang.base.types.message import Part
from toolang.execution.accounting import (
    selected_cost_is_approximate,
    selected_usd_cost,
    token_meter_quantity,
)
from toolang.execution.events import RunBegin, RunEnd, StepBegin, StepEnd
from toolang.execution.types import (
    ModelStepNoted,
    RunStatus,
    StepKind,
    StepRef,
)
from toolang.lang.ast import FlowStmt

from .facts import cost_fact, execution_count_fact, token_fact
from .formatting import flow_statement


@dataclass(frozen=True, slots=True)
class LaneOwner:
    """Nearest parallel Step that owns one descendant Run's presentation."""

    step: StepRef
    lane: int
    item: int
    run_id: str


@dataclass(slots=True)
class LaneState:
    """Semantic state retained for one reusable parallel lane."""

    run_id: str
    item: int
    activity: str = "• starting"
    terminal: tuple[str, ...] = ()
    terminal_status: RunStatus | None = None
    status: RunStatus = "running"
    active: bool = True


@dataclass(slots=True)
class ModelDetail:
    """Append-only streamed content and the current mutable Markdown tail."""

    streamed: str = ""
    pending: str = ""
    pending_gap_before: bool = False
    text_part: int | None = None
    marker_committed: bool = False
    completed_parts: dict[int, Part] = field(default_factory=dict)

    @property
    def lane_preview(self) -> str:
        """Return a bounded suffix for one physical parallel-lane row."""

        return self.streamed[-800:]


@dataclass(slots=True)
class ParDetail:
    """Aggregate and lane state needed only by parallel Flow Steps."""

    lanes: dict[int, LaneState] = field(default_factory=dict)
    total_items: int | None = None
    child_count: int = 0
    active_children: int = 0
    succeeded_children: int = 0
    failed_children: int = 0
    canceled_children: int = 0
    terminating: bool = False


@dataclass(slots=True)
class LoopDetail:
    """Observed iteration state retained until typed terminal facts arrive."""

    iterations: int = 0


@dataclass(frozen=True, slots=True)
class PendingExecute:
    """One model-requested execute awaiting failure or target StepBegin."""

    tool_call_id: str
    runnable: str
    sequence: int
    ready: bool = False


@dataclass(frozen=True, slots=True)
class PlainDetail:
    """A Step with no specialized active projection state."""


StepDetail = ModelDetail | ParDetail | LoopDetail | PlainDetail


def step_detail(kind: StepKind) -> StepDetail:
    """Create only the active state required by one Step kind."""

    if kind == "model":
        return ModelDetail()
    if kind == "par":
        return ParDetail()
    if kind == "loop":
        return LoopDetail()
    return PlainDetail()


@dataclass(slots=True)
class RunState:
    """Active projection state for one Run."""

    begin: RunBegin
    lane_owner: LaneOwner | None
    agic: bool = False
    metrics: Metrics = field(default_factory=lambda: Metrics(runs=1))
    end: RunEnd | None = None
    cancellation_reported: bool = False
    pending_executes: list[PendingExecute] = field(default_factory=list)


@dataclass(slots=True)
class StepState:
    """Common Step projection state plus one typed variant detail."""

    begin: StepBegin
    lane_owner: LaneOwner | None
    ordinal: int
    sequence: int
    detail: StepDetail
    dynamic_run: bool = False
    boundaries: tuple[str, ...] = ()
    metrics: Metrics = field(default_factory=lambda: Metrics())
    cancellation_reported: bool = False
    dynamic_child_run_id: str | None = None

    @property
    def statement(self) -> FlowStmt | None:
        return flow_statement(self.begin.given)

    @property
    def is_flow(self) -> bool:
        return not self.dynamic_run and self.statement is not None

    @property
    def is_dynamic_run(self) -> bool:
        return self.dynamic_run

    @property
    def model(self) -> ModelDetail:
        if not isinstance(self.detail, ModelDetail):
            raise TypeError(f"{self.begin.kind} Step has no Model projection state")
        return self.detail

    @property
    def par(self) -> ParDetail:
        if not isinstance(self.detail, ParDetail):
            raise TypeError(f"{self.begin.kind} Step has no parallel projection state")
        return self.detail

    @property
    def loop(self) -> LoopDetail:
        if not isinstance(self.detail, LoopDetail):
            raise TypeError(f"{self.begin.kind} Step has no loop projection state")
        return self.detail


@dataclass(slots=True)
class Metrics:
    """Aggregate work performed by one Run tree or Flow statement."""

    runs: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_unknown_calls: int = 0
    reasoning_tokens: int = 0
    reasoning_known_calls: int = 0
    cost: Decimal = Decimal("0")
    cost_known: bool = False
    cost_approximate: bool = False

    @property
    def has_activity(self) -> bool:
        """Return whether this metric set owns any visible child work."""

        return any(
            (
                self.runs,
                self.model_calls,
                self.tool_calls,
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.reasoning_tokens,
                self.cost_known,
            )
        )

    def add(self, other: Metrics) -> None:
        self.runs += other.runs
        self.model_calls += other.model_calls
        self.tool_calls += other.tool_calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_unknown_calls += other.cache_unknown_calls
        self.reasoning_tokens += other.reasoning_tokens
        self.reasoning_known_calls += other.reasoning_known_calls
        self.cost += other.cost
        self.cost_known = self.cost_known or other.cost_known
        self.cost_approximate = self.cost_approximate or other.cost_approximate

    def record_step(self, event: StepEnd) -> None:
        if event.kind == "model":
            self.model_calls += 1
            noted = event.noted if isinstance(event.noted, ModelStepNoted) else None
            if noted is not None and noted.accounting is not None:
                accounting = noted.accounting
                self.input_tokens += accounting.input_tokens
                self.output_tokens += accounting.output_tokens
                cache = token_meter_quantity(accounting, "input.cache_read")
                if cache is None:
                    self.cache_unknown_calls += 1
                else:
                    self.cache_read_tokens += cache
                reasoning = token_meter_quantity(accounting, "output.reasoning")
                if reasoning is not None:
                    self.reasoning_tokens += reasoning
                    self.reasoning_known_calls += 1
                selected = selected_usd_cost(accounting)
                if selected is not None:
                    self.cost += selected
                    self.cost_known = True
                self.cost_approximate = (
                    self.cost_approximate or selected_cost_is_approximate(accounting)
                )
            elif noted is not None:
                if noted.tokens:
                    self.input_tokens += noted.tokens.input
                    self.output_tokens += noted.tokens.output
                self.cache_unknown_calls += 1
                if noted.cost:
                    self.cost += Decimal(noted.cost)
                    self.cost_known = True
                self.cost_approximate = True
        elif event.kind == "tool":
            self.tool_calls += 1

    def facts(
        self,
        *,
        duration: str = "",
        include_runs: bool = True,
        include_cost: bool = True,
        run_count: int | None = None,
    ) -> list[str]:
        facts = [duration]
        activity = execution_count_fact(
            (self.runs if run_count is None else run_count) if include_runs else 0,
            self.model_calls,
            self.tool_calls,
        )
        if activity:
            facts.append(activity)
        usage = (
            token_fact(
                self.input_tokens,
                self.output_tokens,
                cache_read_tokens=(
                    self.cache_read_tokens
                    if self.cache_unknown_calls == 0 and self.input_tokens > 0
                    else None
                ),
                reasoning_tokens=(
                    self.reasoning_tokens if self.reasoning_known_calls else None
                ),
                reasoning_complete=(self.reasoning_known_calls == self.model_calls),
            )
            if self.input_tokens or self.output_tokens
            else ""
        )
        cost = (
            cost_fact(self.cost, approximate=self.cost_approximate)
            if include_cost and self.cost_known
            else ""
        )
        facts.extend(value for value in (usage, cost) if value)
        return [fact for fact in facts if fact]
