"""Script-owned root Run framing around shared execution progress."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from toolang.base.types.message import Part
from toolang.execution.events import RunBegin, RunEnd, StepEnd

from ..execution_progress.formatting import (
    count,
    elapsed,
    part_lines,
    runnable_label,
    shape_label,
    status_label,
    value_summary,
)
from ..execution_progress.state import Metrics
from .console import ProgressConsole, Tone


@dataclass(slots=True)
class RunBlock:
    """Root Run header and summary, which remain outside progress semantics."""

    run_id: str
    kind: str
    name: str
    started_at: str
    metrics: Metrics = field(default_factory=lambda: Metrics(runs=1))
    status: str = "running"

    @classmethod
    def from_event(cls, event: RunBegin) -> RunBlock:
        kind, separator, name = event.runnable.partition(":")
        return cls(
            run_id=event.run,
            kind=kind if separator else "run",
            name=runnable_label(name if separator else event.runnable),
            started_at=event.started_at,
        )

    def finish(self, event: RunEnd) -> None:
        self.status = event.status

    def render_header(
        self,
        console: ProgressConsole,
        *,
        verbosity: int,
        kind: str,
        name: str,
        doc: str,
        input_value: tuple[Part, ...],
        args: Mapping[str, object],
        control_index: int,
    ) -> None:
        label = " ".join(
            value for value in (kind or self.kind, name or self.name) if value
        )
        console.write(f"Run {label}")
        if verbosity >= 1 and doc:
            console.wrapped(doc, prefix="")
        console.blank()
        if verbosity < 2:
            return
        input_lines = part_lines(input_value)
        if input_lines:
            console.wrapped(input_lines[0], prefix="> ", continuation="  ")
            for line in input_lines[1:]:
                console.wrapped(line, prefix="  ")
        for arg_name, value in args.items():
            console.write(f"  {arg_name}={value_summary(value)}")
        console.write(f"  {self.run_id}@{control_index}")
        console.blank()

    def render_result(
        self,
        console: ProgressConsole,
        event: RunEnd,
        *,
        output: StepEnd | None,
        error: str,
    ) -> None:
        console.clear_live()
        console.blank()
        tone = _tone(event.status)
        title = f"--- {event.run} {status_label(event.status)} ---"
        console.write(title, tone=tone)
        if event.status == "succeeded" and output is not None:
            if shape := shape_label(output):
                console.write(f"{shape} returned")
        elif error:
            console.wrapped(error, prefix="", tone=tone)
        duration = elapsed(self.started_at, event.finished_at)
        facts = self.metrics.facts(
            duration=duration,
            include_runs=False,
            include_cost=False,
        )
        if self.metrics.runs > 1:
            facts.insert(1 if duration else 0, count(self.metrics.runs - 1, "run"))
        if facts:
            console.wrapped(" · ".join(facts), prefix="")
        console.write("-" * len(title), tone=tone)


def _tone(status: str) -> Tone:
    if status == "failed":
        return "error"
    if status == "canceled":
        return "warning"
    return "progress"
