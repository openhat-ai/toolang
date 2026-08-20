"""Mutable blocks for terminal chat."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import json
import re
from typing import Any

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from toolang.base.types.message import Part, TextPart
from toolang.execution.events import RunBegin, RunEnd, RunEvent, StepBegin, StepEnd
from toolang.execution.types import ExecutionError

from toolang.cli.common.execution_progress import ProgressBlock, ProgressRow
from toolang.cli.common.execution_progress.config import DEFAULT_MAX_PROGRESS_WIDTH
from toolang.cli.common.execution_progress.formatting import (
    count,
    elapsed,
    output_parts,
    shape_label,
    split_hanging_prefix,
    status_label,
)
from toolang.cli.common.execution_progress.state import Metrics

from .base import friendly_error
from .rendering import (
    bar,
    display_len,
    markdown_width,
    render_segments,
    terminal_width,
    truncate_display,
)

STEER_BAR_BG = "#2f555d"


def _terminal_diagnostic(status: str, error: ExecutionError) -> str:
    value = friendly_error(error) if error else ""
    normalized = re.sub(r"[\s._-]+", " ", value.casefold()).strip(" .:!")
    if status == "canceled" and normalized in {
        "canceled",
        "cancelled",
        "run canceled",
        "run cancelled",
        "operation canceled",
        "operation cancelled",
        "interrupted by user",
    }:
        return ""
    return value


def _wrap_plain_lines(text: str) -> list[str]:
    width = max(terminal_width() - 2, 20)
    lines: list[str] = []
    for raw_line in text.splitlines() or [""]:
        line = raw_line.strip()
        while len(line) > width:
            split_at = line.rfind(" ", 0, width + 1)
            split_at = width if split_at <= 0 else split_at
            lines.append(line[:split_at].rstrip())
            line = line[split_at:].lstrip()
        if line:
            lines.append(line)
    return lines


class MutableBlock:
    """A live UI block that the TUI can later move into scrollback."""

    @property
    def type(self) -> str:
        return self.__class__.__name__

    def update(self, event: Any) -> None:
        raise NotImplementedError

    def render(self) -> RenderableType | None:
        raise NotImplementedError


@dataclass(slots=True)
class ExecutionProgressBlock(MutableBlock):
    """One shared finalized or replaceable execution progress block."""

    progress: ProgressBlock
    live: bool = False
    max_width: int = DEFAULT_MAX_PROGRESS_WIDTH

    def update(self, event: Any) -> None:
        if isinstance(event, ProgressBlock):
            self.progress = event

    def render(self) -> RenderableType:
        return Group(
            *(
                _ExecutionProgressRow(
                    row=row,
                    live=self.live,
                    max_width=self.max_width,
                )
                for row in self.progress.rows
            )
        )


@dataclass(frozen=True, slots=True)
class _ExecutionProgressRow:
    """Render one semantic progress row with a stable hanging indent."""

    row: ProgressRow
    live: bool
    max_width: int = DEFAULT_MAX_PROGRESS_WIDTH

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        style = {
            "progress": "dim",
            "normal": "none",
            "active": "none",
            "error": "red",
            "warning": "yellow",
        }[self.row.tone]
        width = max(1, min(options.max_width, self.max_width))
        if self.live and not self.row.wrap_live:
            yield Text(
                truncate_display(self.row.text, width=width),
                style=style,
                no_wrap=True,
            )
            return

        prefix, content = split_hanging_prefix(self.row.text)
        prefix_width = display_len(prefix)
        if prefix_width >= width:
            lines = Text(self.row.text, style=style).wrap(
                console,
                width,
                overflow="fold",
            )
            for line in lines:
                line.rstrip()
                yield Text(line.plain, style=style, no_wrap=True)
            return

        lines = Text(content, style=style).wrap(
            console,
            width - prefix_width,
            overflow="fold",
        )
        if not lines:
            lines.append(Text("", style=style))
        continuation = " " * prefix_width
        for index, line in enumerate(lines):
            line.rstrip()
            yield Text(
                f"{prefix if index == 0 else continuation}{line.plain}",
                style=style,
                no_wrap=True,
            )


@dataclass(slots=True)
class RunStartBlock(MutableBlock):
    """Created by a local submission and finalized by run_begin/run_end."""

    message: str
    run_id: str = ""

    @classmethod
    def create(cls, message: str) -> RunStartBlock:
        return cls(message=message)

    def update(self, event: RunEvent) -> None:
        if isinstance(event, (RunBegin, RunEnd)):
            self.run_id = event.run or self.run_id

    def render(self) -> RenderableType:
        lines: list[RenderableType] = [bar([], style="white on grey23")]
        for index, line in enumerate(self.message.splitlines() or [""]):
            lines.append(
                bar([(">", "grey70 on grey23"), (f" {line}", "white on grey23")])
                if index == 0
                else bar([(f"  {line}", "white on grey23")], style="white on grey23")
            )
        if self.run_id:
            lines.append(
                bar(
                    [(f"  {self.run_id}", "grey70 on grey23")],
                    style="white on grey23",
                )
            )
        else:
            lines.append(bar([], style="white on grey23"))
        lines.append(Text("\n"))
        return Group(*lines)


@dataclass(frozen=True, slots=True)
class SubmissionErrorBlock(MutableBlock):
    """A rejected submission diagnostic with no associated Run."""

    error: str

    def update(self, event: Any) -> None:
        del event

    def render(self) -> RenderableType:
        lines = [
            Text.from_markup(f"[red]• {escape(line)}[/]")
            for line in _wrap_plain_lines(friendly_error(self.error))
        ]
        lines.append(Text("\n"))
        return Group(*lines)


@dataclass(slots=True)
class RunSteerBlock(MutableBlock):
    """Created by a local steer and moved by the next Step or Run end."""

    message: str
    run_id: str = ""
    pending: bool = True

    @classmethod
    def create(cls, *, message: str, run_id: str) -> RunSteerBlock:
        return cls(message=message, run_id=run_id)

    def update(self, event: StepBegin | RunEnd) -> None:
        del event
        self.pending = False

    def render(self) -> RenderableType:
        footer = "  pending for next step" if self.pending else ""
        bg = STEER_BAR_BG
        lines: list[RenderableType] = [Text(), bar([], style=f"white on {bg}")]
        for index, line in enumerate(self.message.splitlines() or [""]):
            lines.append(
                bar(
                    [("+", f"grey70 on {bg}"), (f" {line}", f"white on {bg}")],
                    style=f"white on {bg}",
                )
                if index == 0
                else bar([(f"  {line}", f"white on {bg}")], style=f"white on {bg}")
            )
        lines.extend(
            [bar([(footer, f"grey70 on {bg}")], style=f"white on {bg}"), Text("\n")]
        )
        return Group(*lines)


@dataclass(slots=True)
class RunStopBlock(MutableBlock):
    """Created by Run begin and finalized by Run end."""

    run_id: str
    status: str
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    metrics: Metrics = field(default_factory=Metrics)

    @classmethod
    def create(cls, event: RunBegin | RunEnd) -> RunStopBlock:
        if isinstance(event, RunBegin):
            return cls(
                run_id=event.run or "run",
                status="running",
                started_at=event.started_at,
            )
        return cls(
            run_id=event.run or "run",
            status=event.status,
            error=friendly_error(event.error) if event.error else "",
            finished_at=event.finished_at,
        )

    def update(self, event: RunBegin | RunEnd) -> None:
        self.run_id = event.run or self.run_id
        if isinstance(event, RunBegin):
            self.status = "running"
            self.error = ""
            return
        self.status = event.status
        self.error = friendly_error(event.error) if event.error else self.error
        self.finished_at = event.finished_at

    def set_metrics(self, metrics: Metrics) -> None:
        self.metrics = metrics

    def mark_canceling(self) -> None:
        self.status = "canceling"
        self.error = ""

    def render(self) -> RenderableType:
        if self.status == "running":
            return Text("\n")
        if self.status == "canceling":
            return Text.from_markup("[dim]canceling...[/]")

        tone = (
            "green"
            if self.status == "succeeded"
            else "red"
            if self.status == "failed"
            else "yellow"
            if self.status == "canceled"
            else "dim"
        )
        lines: list[RenderableType] = []
        if message := _terminal_diagnostic(self.status, self.error):
            lines.extend(
                Text.from_markup(f"[{tone}]• {escape(line)}[/]")
                for line in _wrap_plain_lines(message)
            )
        facts = self._facts()
        suffix = f" · {' · '.join(facts)}" if facts else ""
        summary = Text()
        marker = {
            "succeeded": "✔",
            "failed": "✘",
            "canceled": "⁃",
        }.get(self.status, "◆")
        summary.append(f"{marker} ", style=tone)
        summary.append(f"{self.run_id} ", style="dim")
        summary.append(status_label(self.status), style="dim")
        summary.append(suffix, style="dim")
        lines.extend([Text(), summary, Text("\n")])
        return Group(*lines)

    def _facts(self) -> list[str]:
        duration = elapsed(self.started_at, self.finished_at)
        facts = self.metrics.facts(
            duration=duration,
            include_runs=False,
        )
        if self.metrics.runs > 1:
            facts.insert(
                1 if duration else 0,
                count(self.metrics.runs - 1, "run"),
            )
        return facts


@dataclass(frozen=True, slots=True)
class AssistantResponseBlock(MutableBlock):
    """One durable response requested independently of live progress."""

    text: str
    shape: str = ""

    @classmethod
    def create(cls, event: StepEnd) -> AssistantResponseBlock:
        return cls(text=_parts_text(output_parts(event)), shape=shape_label(event))

    @classmethod
    def from_parts(cls, parts: Sequence[Part]) -> AssistantResponseBlock:
        text = _parts_text(parts)
        if not text and parts:
            text = json.dumps(
                [part.to_data() for part in parts],
                ensure_ascii=False,
                indent=2,
            )
        return cls(text=text)

    def update(self, event: Any) -> None:
        del event

    def render(self) -> RenderableType | None:
        if self.text:
            return _render_markdown_output(self.text.splitlines())
        if self.shape:
            return Text.from_markup(f"[dim]• {escape(f'{self.shape} returned')}[/]")
        return None


@dataclass(frozen=True, slots=True)
class SlashResultBlock:
    """Render a structured slash-command result with a terminal boundary."""

    parts: Sequence[Part]

    def render(self) -> RenderableType:
        response = AssistantResponseBlock.from_parts(self.parts).render()
        if response is None:
            return Text("\n")
        return Group(response, Text("\n"))


@dataclass(frozen=True, slots=True)
class HeaderBlock:
    model_label: str
    home: str
    version_label: str

    def render(self) -> RenderableType:
        rows = [
            Text.from_markup(
                f"[dim]T··⅃ [/][bold]Toolang[/][dim] (v{escape(self.version_label)})[/]"
            ),
            Text(),
            Text.from_markup(f"[none]model: {escape(self.model_label)}[/]"),
            Text.from_markup(f"[none]home:  {escape(self.home)}[/]"),
        ]
        content = Table.grid(padding=0)
        content.add_column(no_wrap=True)
        for row in rows:
            content.add_row(row)
        return Group(
            Panel(
                content,
                box=box.ROUNDED,
                border_style="dim",
                padding=(0, 1),
                width=max(row.cell_len for row in rows) + 4,
            ),
            Text("\n"),
        )


@dataclass(frozen=True, slots=True)
class SlashBlock:
    message: str
    body: Sequence[str]

    def render(self) -> RenderableType:
        lines: list[RenderableType] = [bar([], style="white on grey23")]
        lines.extend(
            bar(
                [(">", "grey70 on grey23"), (f" {line}", "white on grey23")]
                if index == 0
                else [(f"  {line}", "white on grey23")]
            )
            for index, line in enumerate(self.message.splitlines() or [""])
        )
        lines.extend([bar([], style="white on grey23"), Text()])
        if self.body:
            first, *rest = self.body
            lines.append(Text.from_markup(f"[dim]:[/] [none]{escape(first)}[/]"))
            lines.append(Text())
            if rest and not rest[0].strip():
                rest = rest[1:]
            lines.extend(self._body_line(line) for line in rest)
        lines.append(Text("\n"))
        return Group(*lines)

    @staticmethod
    def _body_line(line: str) -> Text:
        if not line.strip():
            return Text()
        if line.startswith(":"):
            return SlashBlock._command_line(line)
        columns = _split_columns(line)
        if len(columns) > 1:
            return SlashBlock._table_line(columns)
        return Text.from_markup(f"[none]  {escape(line)}[/]")

    @staticmethod
    def _command_line(line: str) -> Text:
        usage, _, summary = line.partition("  ")
        while summary.startswith(" "):
            summary = summary[1:]
        text = Text("  ")
        SlashBlock._append_usage(text, usage)
        if summary:
            text.append(" " * max(2, 34 - text.cell_len))
            text.append(summary, style="none")
        return text

    @staticmethod
    def _table_line(columns: Sequence[str]) -> Text:
        text = Text("  ")
        first, *rest = columns
        badge = rest[0] if rest and rest[0] in {"current", "default"} else ""
        details = rest[1:] if badge else rest
        text.append(first, style="cyan")
        text.append(" " * max(2, 40 - display_len(first)))
        if badge:
            text.append(badge, style="yellow" if badge == "default" else "dim")
        text.append(" " * max(2, 9 - display_len(badge)))
        for index, column in enumerate(details):
            text.append(column, style="dim")
            if index < len(details) - 1:
                text.append("  ", style="none")
        return text

    @staticmethod
    def _append_usage(text: Text, usage: str) -> None:
        for index, token in enumerate(usage.split(" ")):
            if index:
                text.append(" ")
            style = "cyan" if token.startswith(":") else "dim"
            if token.startswith(":"):
                command, separator, rest = token.partition(",")
                text.append(command, style=style)
                if separator:
                    text.append(separator, style="dim")
                    text.append(
                        rest,
                        style="cyan" if rest.startswith(":") else "dim",
                    )
            else:
                text.append(token, style=style)


def _render_markdown_output(lines: Sequence[str]) -> RenderableType:
    rows: list[list[tuple[str, Any]]] = [[]]
    width = max(20, markdown_width() - 2)
    for segment in render_segments(Markdown("\n".join(lines)), width=width):
        if segment.control or not segment.text:
            continue
        for index, part in enumerate(segment.text.split("\n")):
            if index:
                rows.append([])
            if part:
                rows[-1].append((part, segment.style))

    while rows and not any(text.strip() for text, _style in rows[0]):
        rows.pop(0)
    while rows and not any(text.strip() for text, _style in rows[-1]):
        rows.pop()
    if not rows:
        return Text.from_markup("[none]•[/]")

    rendered_rows: list[Text] = []
    for index, row in enumerate(rows):
        line = Text("• " if index == 0 else "  ")
        for text, style in row:
            line.append(text, style=style)
        rendered_rows.append(line)
    return Group(*rendered_rows)


def _split_columns(line: str) -> list[str]:
    return [part for part in re.split(r" {2,}", line.strip()) if part]


def _parts_text(parts: Sequence[Part]) -> str:
    return "".join(part.text for part in parts if isinstance(part, TextPart)).strip()
