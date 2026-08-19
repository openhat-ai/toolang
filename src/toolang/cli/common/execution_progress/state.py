"""Shared execution progress metrics."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from toolang.execution.events import StepEnd
from toolang.execution.types import ModelStepNoted

from .formatting import count, token_fact


@dataclass(slots=True)
class Metrics:
    """Aggregate work performed by one Run tree or Flow statement."""

    runs: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Decimal = Decimal("0")

    def add(self, other: Metrics) -> None:
        self.runs += other.runs
        self.model_calls += other.model_calls
        self.tool_calls += other.tool_calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost += other.cost

    def record_step(self, event: StepEnd) -> None:
        if event.kind == "model":
            self.model_calls += 1
            if isinstance(event.noted, ModelStepNoted) and event.noted.tokens:
                self.input_tokens += event.noted.tokens.input
                self.output_tokens += event.noted.tokens.output
            if isinstance(event.noted, ModelStepNoted) and event.noted.cost:
                self.cost += Decimal(event.noted.cost)
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
        if self.input_tokens or self.output_tokens:
            facts.append(token_fact(self.input_tokens, self.output_tokens))
        if include_cost and self.cost:
            facts.append(f"${self.cost}")
        return [fact for fact in facts if fact]
