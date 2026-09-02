"""Mutable blocks for terminal chat."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import re
from typing import Any, Literal

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.markup import escape
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from toolang.base.types.message import Part
from toolang.cli.common.output import toolang_logo, toolang_logo_text
from toolang.execution.events import RunBegin, RunEnd, RunEvent, StepBegin, StepEnd
from toolang.execution.types import ExecutionError

from toolang.cli.common.execution_progress import ProgressBlock
from toolang.cli.common.execution_progress.config import DEFAULT_MAX_PROGRESS_WIDTH
from toolang.cli.common.execution_progress.facts import elapsed_fact
from toolang.cli.common.execution_progress.formatting import (
    display_width,
    output_parts,
    shape_label,
    truncate,
    wrap_display,
)
from toolang.cli.common.execution_progress.rich_rendering import (
    RUN_DIVIDER_WIDTH,
    progress_block_renderable,
    run_footer_renderable,
    terminal_status_style,
)
from toolang.cli.common.execution_progress.state import Metrics
from toolang.cli.common.human_values import parts_response_text, response_renderable

from .base import ChatExecutorMetadata, friendly_error
from .rendering import (
    CONTROL_BAR_BACKGROUND,
    ACCENT_CELL,
    QUICK_COMMAND_CONTROL_ACCENT,
    RUN_CONTROL_ACCENT,
    STEER_CONTROL_ACCENT,
    bar,
    terminal_width,
)
from .slashes import SlashTable
from .tables import table_lines

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
    width: int | None = None,
) -> Text:
    background = CONTROL_BAR_BACKGROUND
    return bar(
        [
            (ACCENT_CELL, f"not dim on {accent}"),
            (
                f" {content}" if content else "",
                f"not dim on {background}",
            ),
        ],
        style=f"not dim on {background}",
        width=width,
    )


def _control_bar_lines(message: str, *, accent: str) -> list[RenderableType]:
    width = terminal_width()
    content_width = max(1, width - 2)
    wrapped_lines = [
        wrapped_line
        for line in message.splitlines() or [""]
        for wrapped_line in wrap_display(line, content_width)
    ]
    lines: list[RenderableType] = [
        _control_bar_line(line, accent=accent, width=width) for line in wrapped_lines
    ]
    padding_count = max(0, 3 - len(lines))
    top_padding_count = (padding_count + 1) // 2
    bottom_padding_count = padding_count - top_padding_count
    return [
        *(
            _control_bar_line(accent=accent, width=width)
            for _ in range(top_padding_count)
        ),
        *lines,
        *(
            _control_bar_line(accent=accent, width=width)
            for _ in range(bottom_padding_count)
        ),
    ]


def _slash_control_lines(message: str) -> list[RenderableType]:
    return [
        *_control_bar_lines(
            message,
            accent=QUICK_COMMAND_CONTROL_ACCENT,
        ),
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
class RunControlBlock(MutableBlock):
    """Created by a local submission and finalized by run_begin/run_end."""

    message: str

    @classmethod
    def create(cls, message: str) -> RunControlBlock:
        return cls(message=message)

    def update(self, event: RunEvent) -> None:
        del event

    def render(self) -> RenderableType:
        return Group(
            *_control_bar_lines(
                self.message,
                accent=RUN_CONTROL_ACCENT,
            ),
            Text(),
        )


@dataclass(frozen=True, slots=True)
class SubmissionErrorBlock(MutableBlock):
    """A rejected submission diagnostic with no associated Run."""

    error: str

    def update(self, event: Any) -> None:
        del event

    def render(self) -> RenderableType:
        lines: list[RenderableType] = [
            Text(),
            *(
                Text.from_markup(f"[red]• {escape(line)}[/]")
                for line in _wrap_plain_lines(friendly_error(self.error))
            ),
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
        lines: list[RenderableType] = [Text()]
        lines.extend(
            _control_bar_lines(
                self.message,
                accent=STEER_CONTROL_ACCENT,
            )
        )
        lines.append(Text())
        return Group(*lines)


@dataclass(slots=True)
class RunSummaryBlock(MutableBlock):
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
    ) -> RunSummaryBlock:
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
        duration = elapsed_fact(self.started_at, self.finished_at)
        facts = self.metrics.facts(
            duration=duration,
            run_count=max(self.metrics.runs - 1, 0),
        )
        return facts


@dataclass(frozen=True, slots=True)
class AssistantResponseBlock(MutableBlock):
    """One durable response requested independently of live progress."""

    text: str
    shape: str = ""
    max_width: int = DEFAULT_MAX_PROGRESS_WIDTH

    @classmethod
    def create(cls, event: StepEnd) -> AssistantResponseBlock:
        return cls(
            text=parts_response_text(output_parts(event)),
            shape=shape_label(event),
        )

    @classmethod
    def from_parts(
        cls,
        parts: Sequence[Part],
        *,
        max_width: int = DEFAULT_MAX_PROGRESS_WIDTH,
    ) -> AssistantResponseBlock:
        return cls(text=parts_response_text(parts), max_width=max_width)

    def update(self, event: Any) -> None:
        del event

    def render(self) -> RenderableType | None:
        if self.text:
            return response_renderable(self.text, max_width=self.max_width)
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
        response = AssistantResponseBlock.from_parts(
            self.parts,
            max_width=self.max_width,
        ).render()
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
    executor_metadata: ChatExecutorMetadata
    version_label: str

    def render(self) -> RenderableType:
        return Group(Text(), self, Text("\n"))

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        details = Table.grid(padding=(0, _HEADER_FIELD_GAP))
        details.add_column(no_wrap=True)
        details.add_column(no_wrap=False, overflow="fold")
        executor_value = _header_executor_value(
            self.executor_metadata,
            tui_version=self.version_label,
        )
        details.add_row(Text("executor", style="dim"), executor_value)
        sandbox_value = _header_sandbox_value(self.executor_metadata)
        details.add_row(Text("sandbox", style="dim"), sandbox_value)
        details.add_row(Text("home", style="dim"), Text(self.home))

        logo_text = toolang_logo_text()
        logo = toolang_logo(console)
        logo_width = max(display_width(line) for line in logo_text.splitlines())
        details_width = (
            display_width("executor")
            + _HEADER_FIELD_GAP
            + max(
                display_width(self.home),
                display_width(executor_value.plain),
                display_width(sandbox_value.plain),
            )
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
            title_align="left",
            title=Text(
                f"Toolang {self.version_label}",
                style=Style(bold=False, dim=False),
            ),
        )


def _header_executor_value(
    metadata: ChatExecutorMetadata,
    *,
    tui_version: str,
) -> Text:
    if metadata.endpoint is None:
        return Text("embedded")
    if metadata.version is None:
        raise ValueError("remote chat executor metadata is missing its version")
    executor = Text()
    executor.append(metadata.endpoint, style=Style(link=metadata.endpoint))
    if not _versions_confirmed_equal(metadata.version, tui_version):
        executor.append(" · ", style="dim")
        executor.append(metadata.version)
    return executor


def _header_sandbox_value(metadata: ChatExecutorMetadata) -> Text:
    sandbox = Text(metadata.sandbox_selector)
    sandbox.append(" · ", style="dim")
    sandbox.append(metadata.sandbox_detail)
    return sandbox


def _versions_confirmed_equal(executor_version: str, tui_version: str) -> bool:
    return (
        executor_version == tui_version
        and executor_version != "unknown"
        and not executor_version.endswith("*")
    )


@dataclass(frozen=True, slots=True)
class SlashBlock:
    message: str
    body: Sequence[str]
    kind: Literal["success", "result", "usage", "error"] = "result"

    def render(self) -> RenderableType:
        lines = _slash_control_lines(self.message)
        if self.body:
            first, *rest = self.body
            lines.append(self._summary_line(first))
            lines.append(Text())
            if rest and not rest[0].strip():
                rest = rest[1:]
            lines.extend(self._body_line(line) for line in rest)
        lines.append(Text("\n"))
        return Group(*lines)

    def _summary_line(self, line: str) -> Text:
        styles = {
            "success": "green",
            "result": "none",
            "usage": "yellow",
            "error": "red",
        }
        text = Text("  ")
        text.append(line, style=styles[self.kind])
        return text

    @staticmethod
    def _body_line(line: str) -> Text:
        if not line.strip():
            return Text()
        if line.startswith(("/", ":")):
            return SlashBlock._command_line(line)
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
    def _append_usage(text: Text, usage: str) -> None:
        for index, token in enumerate(usage.split(" ")):
            if index:
                text.append(" ")
            is_command = token.startswith(("/", ":"))
            style = "cyan" if is_command else "dim"
            if is_command:
                command, separator, rest = token.partition(",")
                text.append(command, style=style)
                if separator:
                    text.append(separator, style="dim")
                    text.append(
                        rest,
                        style=("cyan" if rest.startswith(("/", ":")) else "dim"),
                    )
            else:
                text.append(token, style=style)


@dataclass(frozen=True, slots=True)
class SlashTableBlock:
    """Render one submitted slash command with a structured result table."""

    message: str
    table: SlashTable

    def render(self) -> RenderableType:
        summary = Text("  ")
        summary.append(self.table.summary)
        return Group(
            *_slash_control_lines(self.message),
            summary,
            Text(),
            _SlashTableRows(self.table),
            Text("\n"),
        )


@dataclass(frozen=True, slots=True)
class _SlashTableRows:
    table: SlashTable

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        del console
        lines = table_lines(
            self.table.headers,
            self.table.rows,
            width=max(1, options.max_width - 2),
            shrink_order=self.table.shrink_order,
            protected_suffixes=self.table.protected_suffixes,
        )
        for index, line in enumerate(lines):
            rendered = Text("  ")
            rendered.append(line, style="dim" if index == 1 else "none")
            rendered.no_wrap = True
            yield rendered
