"""Typed active state for execution progress projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from toolang.execution.events import RunBegin, RunEnd, StepBegin, StepEnd
from toolang.execution.types import ModelStepNoted, StepKind, StepPath
from toolang.lang.ast import FlowStmt

from .formatting import count, flow_statement, token_fact


@dataclass(frozen=True, slots=True)
class LaneOwner:
    """Nearest parallel Step that owns one descendant Run's presentation."""

    step: StepPath
    lane: int
    item: int
    run_id: str


@dataclass(slots=True)
class LaneState:
    """Semantic state retained for one reusable parallel lane."""

    run_id: str
    item: int
    activity: str = "· starting…"
    terminal: tuple[str, ...] = ()
    status: str = "running"
    active: bool = True


@dataclass(slots=True)
class ModelDetail:
    """Bounded streaming state needed only by Model Steps."""

    preview: str = ""


@dataclass(slots=True)
class ParDetail:
    """Aggregate and lane state needed only by parallel Flow Steps."""

    lanes: dict[int, LaneState] = field(default_factory=dict)
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
    metrics: Metrics = field(default_factory=lambda: Metrics(runs=1))
    end: RunEnd | None = None

    @property
    def kind(self) -> str:
        kind, separator, _name = self.begin.runnable.partition(":")
        return kind if separator else "run"


@dataclass(slots=True)
class StepState:
    """Common Step projection state plus one typed variant detail."""

    begin: StepBegin
    lane_owner: LaneOwner | None
    ordinal: int
    sequence: int
    detail: StepDetail
    boundaries: tuple[str, ...] = ()
    metrics: Metrics = field(default_factory=lambda: Metrics())

    @property
    def statement(self) -> FlowStmt | None:
        return flow_statement(self.begin.given)

    @property
    def is_flow(self) -> bool:
        return self.statement is not None

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
    cost: Decimal = Decimal("0")
    cost_known: bool = False

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
                self.cost_known,
            )
        )

    def add(self, other: Metrics) -> None:
        self.runs += other.runs
        self.model_calls += other.model_calls
        self.tool_calls += other.tool_calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost += other.cost
        self.cost_known = self.cost_known or other.cost_known

    def record_step(self, event: StepEnd) -> None:
        if event.kind == "model":
            self.model_calls += 1
            if isinstance(event.noted, ModelStepNoted) and event.noted.tokens:
                self.input_tokens += event.noted.tokens.input
                self.output_tokens += event.noted.tokens.output
            if isinstance(event.noted, ModelStepNoted) and event.noted.cost:
                self.cost += Decimal(event.noted.cost)
                self.cost_known = True
        elif event.kind == "tool":
            self.tool_calls += 1

    def facts(
        self,
        *,
        duration: str = "",
        include_runs: bool = True,
        include_cost: bool = True,
    ) -> list[str]:
        facts = [duration]
        if include_runs and self.runs:
            facts.append(count(self.runs, "run"))
        if self.model_calls:
            facts.append(count(self.model_calls, "model call"))
        if self.tool_calls:
            facts.append(count(self.tool_calls, "tool call"))
        usage = (
            token_fact(self.input_tokens, self.output_tokens)
            if self.input_tokens or self.output_tokens
            else ""
        )
        cost = ""
        if include_cost and self.cost_known:
            rounded = self.cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            cost = f"${rounded:.2f}"
        if usage or cost:
            facts.append(" ".join(value for value in (usage, cost) if value))
        return [fact for fact in facts if fact]
