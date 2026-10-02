"""Script-owned root Run framing around shared execution progress."""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.console import Console, ConsoleOptions, RenderResult
from rich.text import Text

from toolang.base.types.model import ModelRequest
from toolang.execution.events import RunBegin, RunEnd
from toolang.lang.types import display_runnable_ref

from ..execution_progress.facts import elapsed_fact
from ..execution_progress.formatting import display_width
from ..execution_progress.rich_rendering import run_footer_renderable
from ..execution_progress.state import Metrics
from ..model_formatting import model_reasoning_value
from .console import ProgressConsole


@dataclass(frozen=True, slots=True)
class RunContext:
    """Initial resolved Script bindings, rendered outside shared Step progress."""

    runnable: str
    model: ModelRequest | None

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        width = max(1, options.max_width)
        runnable = _context_text(
            display_runnable_ref(self.runnable, surface="chat")
            or "runnable unspecified"
        )
        fields = ["model unspecified"]
        if self.model is not None:
            effort = model_reasoning_value(self.model) or "auto"
            fields = [_context_text(self.model.ref), _context_text(effort)]
        prefix = "‣ " if width > 2 else ""
        left = prefix + runnable
        right = " · ".join(fields)
        gap = width - display_width(left) - display_width(right)
        if gap >= 2:
            yield Text(left + " " * gap + right, style="dim", no_wrap=True)
            return

        indent = " " * len(prefix)
        body_width = width - len(prefix)
        for index, line in enumerate(_wrap_context(runnable, console, body_width)):
            yield Text(
                (prefix if index == 0 else indent) + line,
                style="dim",
                no_wrap=True,
            )
        if display_width(right) <= body_width:
            fields = [right]
        for value in fields:
            for line in _wrap_context(value, console, body_width):
                yield Text(indent + line, style="dim", no_wrap=True)


def _context_text(value: str) -> str:
    """Keep terminal controls visible as escaped text, never executable bytes."""

    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in value)


def _wrap_context(value: str, console: Console, width: int) -> list[str]:
    # Escape a wide character when even one glyph cannot fit the available cell.
    value = "".join(
        char if display_width(char) <= width else ascii(char)[1:-1] for char in value
    )
    return [line.plain for line in Text(value).wrap(console, width, overflow="fold")]


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
        duration = elapsed_fact(self.started_at, event.finished_at)
        facts = self.metrics.facts(
            duration=duration,
            run_count=max(self.metrics.runs - 1, 0),
        )
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
