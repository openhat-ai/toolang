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
from toolang.cli.common.output import toolang_logo, toolang_logo_text
from toolang.execution.events import RunBegin, RunEnd, RunEvent, StepBegin, StepEnd
from toolang.execution.types import ExecutionError

from toolang.cli.common.execution_progress import ProgressBlock
from toolang.cli.common.execution_progress.config import DEFAULT_MAX_PROGRESS_WIDTH
from toolang.cli.common.execution_progress.formatting import (
    count,
    display_width,
    elapsed,
    output_parts,
    shape_label,
    truncate,
)
from toolang.cli.common.execution_progress.rich_rendering import (
    RUN_DIVIDER_WIDTH,
    progress_block_renderable,
    run_footer_renderable,
    terminal_status_style,
)
from toolang.cli.common.execution_progress.state import Metrics

from .base import friendly_error
from .rendering import (
    CONTROL_BAR_BACKGROUND,
    ACCENT_CELL,
    QUICK_COMMAND_CONTROL_ACCENT,
    START_CONTROL_ACCENT,
    STEER_CONTROL_ACCENT,
    bar,
    display_len,
    markdown_width,
    render_segments,
    terminal_width,
)

_HEADER_MIN_WIDE_WIDTH = 69
_HEADER_HORIZONTAL_PADDING = 2
_HEADER_COLUMN_GAP = 4
_HEADER_FIELD_GAP = 2


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


def _control_bar_line(
    content: str = "",
    *,
    accent: str,
) -> Text:
    background = CONTROL_BAR_BACKGROUND
    return bar(
        [
            (ACCENT_CELL, f"on {accent}"),
            (f" {content}" if content else "", f"white on {background}"),
        ],
        style=f"on {background}",
    )


def _slash_control_lines(message: str) -> list[RenderableType]:
    return [
        _control_bar_line(accent=QUICK_COMMAND_CONTROL_ACCENT),
        *(
            _control_bar_line(line, accent=QUICK_COMMAND_CONTROL_ACCENT)
            for line in message.splitlines() or [""]
        ),
        _control_bar_line(accent=QUICK_COMMAND_CONTROL_ACCENT),
        Text(),
    ]


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
    """One shared committed or replaceable execution progress block."""

    progress: ProgressBlock
    live: bool = False
    max_width: int = DEFAULT_MAX_PROGRESS_WIDTH

    def update(self, event: Any) -> None:
        if isinstance(event, ProgressBlock):
            self.progress = event

    def render(self) -> RenderableType:
        rendered = progress_block_renderable(
            self.progress,
            live=self.live,
            max_width=self.max_width,
        )
        if self.live:
            return rendered
        # Chat removes one terminal layout newline from every renderable. The
        # sentinel is removed instead so finalized progress keeps every
        # projected row, including a trailing semantic blank row.
        return Group(rendered, Text())


@dataclass(slots=True)
class RunStartBlock(MutableBlock):
    """Created by a local submission and finalized by run_begin/run_end."""

    message: str

    @classmethod
    def create(cls, message: str) -> RunStartBlock:
        return cls(message=message)

    def update(self, event: RunEvent) -> None:
        del event

    def render(self) -> RenderableType:
        lines: list[RenderableType] = [_control_bar_line(accent=START_CONTROL_ACCENT)]
        lines.extend(
            _control_bar_line(line, accent=START_CONTROL_ACCENT)
            for line in self.message.splitlines() or [""]
        )
        lines.append(_control_bar_line(accent=START_CONTROL_ACCENT))
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

    @classmethod
    def create(cls, *, message: str, run_id: str) -> RunSteerBlock:
        return cls(message=message, run_id=run_id)

    def update(self, event: StepBegin | RunEnd) -> None:
        del event

    def render(self) -> RenderableType:
        lines: list[RenderableType] = [
            Text(),
            _control_bar_line(accent=STEER_CONTROL_ACCENT),
        ]
        lines.extend(
            _control_bar_line(line, accent=STEER_CONTROL_ACCENT)
            for line in self.message.splitlines() or [""]
        )
        lines.extend([_control_bar_line(accent=STEER_CONTROL_ACCENT), Text("\n")])
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
    max_width: int = DEFAULT_MAX_PROGRESS_WIDTH
    gap_before: bool = True

    @classmethod
    def create(
        cls,
        event: RunBegin | RunEnd,
        *,
        max_width: int = DEFAULT_MAX_PROGRESS_WIDTH,
    ) -> RunStopBlock:
        if isinstance(event, RunBegin):
            return cls(
                run_id=event.run or "run",
                status="running",
                started_at=event.started_at,
                max_width=max_width,
            )
        return cls(
            run_id=event.run or "run",
            status=event.status,
            error=friendly_error(event.error) if event.error else "",
            finished_at=event.finished_at,
            max_width=max_width,
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
            return Text.from_markup("[dim]canceling[/]")

        tone = terminal_status_style(self.status)
        lines: list[RenderableType] = []
        if message := _terminal_diagnostic(self.status, self.error):
            lines.extend(
                Text.from_markup(f"[{tone}]• {escape(line)}[/]")
                for line in _wrap_plain_lines(message)
            )
        facts = self._facts()
        lines.extend(
            [
                run_footer_renderable(
                    run_id=self.run_id,
                    status=self.status,
                    facts=facts,
                    max_width=self.max_width,
                    gap_before=self.gap_before,
                ),
                Text("\n"),
            ]
        )
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
    """Render a structured slash-command result with a terminal divider."""

    message: str
    run_id: str
    parts: Sequence[Part]
    max_width: int = DEFAULT_MAX_PROGRESS_WIDTH

    def render(self) -> RenderableType:
        response = AssistantResponseBlock.from_parts(self.parts).render()
        lines = [
            *_slash_control_lines(self.message),
            _SlashResultDivider(self.run_id, max_width=self.max_width),
        ]
        if response is not None:
            lines.extend([Text(), response])
        lines.append(Text("\n"))
        return Group(*lines)


@dataclass(frozen=True, slots=True)
class _SlashResultDivider:
    run_id: str
    max_width: int

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        del console
        width = max(1, min(options.max_width, self.max_width))
        divider_width = min(width, RUN_DIVIDER_WIDTH)
        caption = f"{self.run_id} result"
        if divider_width < 5:
            divider = Text("•", style="dim")
            if divider_width > 1:
                divider.append(" ", style="dim")
            if divider_width > 2:
                divider.append(
                    truncate(caption, divider_width - 2),
                    style="dim",
                )
            divider.no_wrap = True
            yield divider
            return

        caption = truncate(caption, max(divider_width - 4, 1))
        caption_width = display_width(caption)
        divider = Text()
        divider.append("•", style="dim")
        divider.append(" ", style="dim")
        divider.append(caption, style="dim")
        divider.append(" ", style="dim")
        divider.append(
            "─" * max(divider_width - caption_width - 3, 0),
            style="dim",
        )
        divider.no_wrap = True
        yield divider


@dataclass(frozen=True, slots=True)
class HeaderBlock:
    home: str
    version_label: str

    def render(self) -> RenderableType:
        return Group(Text(), self, Text("\n"))

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        identity = Text()
        identity.append("Toolang", style="bold bright_cyan")
        identity.append(f" {self.version_label}")

        fields = Table.grid(padding=(0, _HEADER_FIELD_GAP))
        fields.add_column(no_wrap=True)
        fields.add_column(no_wrap=False, overflow="fold")
        fields.add_row(Text("home", style="dim"), Text(self.home))
        fields.add_row(Text("executor", style="dim"), Text("local"))

        details = Table.grid(padding=0)
        details.add_column(no_wrap=False)
        details.add_row(identity)
        details.add_row(fields)

        logo_text = toolang_logo_text()
        logo = toolang_logo(console)
        logo_width = max(display_width(line) for line in logo_text.splitlines())
        details_width = max(
            display_width("Toolang") + 1 + display_width(self.version_label),
            display_width("executor")
            + _HEADER_FIELD_GAP
            + max(display_width(self.home), display_width("local")),
        )
        wide_width = (
            2
            + 2 * _HEADER_HORIZONTAL_PADDING
            + logo_width
            + _HEADER_COLUMN_GAP
            + details_width
        )
        if options.max_width >= max(_HEADER_MIN_WIDE_WIDTH, wide_width):
            content = Table.grid(padding=(0, _HEADER_COLUMN_GAP))
            content.add_column(no_wrap=True, vertical="top")
            content.add_column(no_wrap=False, vertical="top")
            content.add_row(logo, details)
        else:
            content = Table.grid(padding=0)
            content.add_column(no_wrap=False)
            content.add_row(logo)
            content.add_row(Text())
            content.add_row(details)

        yield Panel(
            content,
            box=box.ROUNDED,
            border_style="dim",
            padding=(1, _HEADER_HORIZONTAL_PADDING),
            expand=False,
        )


@dataclass(frozen=True, slots=True)
class SlashBlock:
    message: str
    body: Sequence[str]

    def render(self) -> RenderableType:
        lines = _slash_control_lines(self.message)
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
