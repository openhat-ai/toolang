"""Script-owned root Run framing around shared execution progress."""

from __future__ import annotations

from dataclasses import dataclass, field

from toolang.execution.events import RunBegin, RunEnd

from ..execution_progress.formatting import (
    count,
    elapsed,
)
from ..execution_progress.rich_rendering import run_footer_renderable
from ..execution_progress.state import Metrics
from .console import ProgressConsole


@dataclass(slots=True)
class RunBlock:
    """Root Run footer outside shared Step progress semantics."""

    run_id: str
    started_at: str
    operation: str | None = None
    metrics: Metrics = field(default_factory=lambda: Metrics(runs=1))

    @classmethod
    def from_event(
        cls,
        event: RunBegin,
        *,
        operation: str | None = None,
    ) -> RunBlock:
        return cls(
            run_id=event.run,
            started_at=event.started_at,
            operation=operation,
        )

    def render_result(
        self,
        console: ProgressConsole,
        event: RunEnd,
        *,
        gap_before: bool,
    ) -> None:
        console.clear_live()
        duration = elapsed(self.started_at, event.finished_at)
        facts = self.metrics.facts(
            duration=duration,
            include_runs=False,
        )
        if self.metrics.runs > 1:
            facts.insert(1 if duration else 0, count(self.metrics.runs - 1, "run"))
        console.write_renderable(
            run_footer_renderable(
                run_id=event.run,
                operation=self.operation,
                status=event.status,
                facts=facts,
                max_width=console.width,
                gap_before=gap_before,
            )
        )
