"""Script-owned root Run framing around shared execution progress."""

from __future__ import annotations

from dataclasses import dataclass, field

from toolang.execution.events import RunBegin, RunEnd

from ..execution_progress.formatting import (
    count,
    elapsed,
    status_label,
)
from ..execution_progress.state import Metrics
from .console import ProgressConsole, Tone


@dataclass(slots=True)
class RunBlock:
    """Root Run footer outside shared Step progress semantics."""

    run_id: str
    started_at: str
    metrics: Metrics = field(default_factory=lambda: Metrics(runs=1))

    @classmethod
    def from_event(cls, event: RunBegin) -> RunBlock:
        return cls(
            run_id=event.run,
            started_at=event.started_at,
        )

    def render_result(
        self,
        console: ProgressConsole,
        event: RunEnd,
    ) -> None:
        console.clear_live()
        console.blank()
        tone = _tone(event.status)
        title = f"--- {event.run} {status_label(event.status)} ---"
        console.write(title, tone=tone)
        duration = elapsed(self.started_at, event.finished_at)
        facts = self.metrics.facts(
            duration=duration,
            include_runs=False,
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
