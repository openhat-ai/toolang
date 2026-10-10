"""Mutable blocks for terminal chat."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import re
from typing import Any, Literal

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.markup import escape
from rich.text import Text

from toolang.base.types.message import Part
from toolang.cli.common.banner import Banner, version_label
from toolang.cli.common.terminal_surfaces import DARK_TERMINAL_SURFACES
from toolang.cli.common.model_formatting import model_reasoning_value
from toolang.execution.events import RunBegin, RunEnd, RunEvent, StepBegin, StepEnd
from toolang.execution.schemas import RunRequest
from toolang.execution.types import ErrorMessage, ErrorRef

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
from toolang.lang.types import display_runnable_ref

from .base import ChatExecutorMetadata, friendly_error
from .rendering import (
    CONTROL_BAR_MARK,
    QUICK_COMMAND_CONTROL_ACCENT,
    RUN_CONTROL_ACCENT,
    STEER_CONTROL_ACCENT,
    bar,
    terminal_width,
)
from .slashes import (
    SlashHelp,
    SlashTable,
    SlashOutcome,
    outcome_lines,
)
from .tables import table_lines


def _terminal_diagnostic(status: str, error: str | ErrorMessage | ErrorRef) -> str:
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
    input_background: str,
    width: int | None = None,
    corner: str = "",
    accent_head: bool = True,
) -> Text:
    output_width = width or terminal_width()
    left = min(2, max(0, output_width - 1))
    right = min(2, max(0, output_width - left - 1))
    if not left:
        head: tuple[str, str] = ("", "")
    elif accent_head:
        head = (CONTROL_BAR_MARK, f"{accent} not dim on {input_background}")
    else:
        head = (" ", f"not dim on {input_background}")
    line = bar(
        [
            head,
            (
                " " * max(0, left - 1) + content if content else "",
                f"not dim on {input_background}",
            ),
        ],
        style=f"not dim on {input_background}",
        width=output_width,
    )
    if corner:
        corner = truncate(corner, max(0, output_width - left - right))
        start = output_width - right - display_width(corner)
        line = line[:start]
        line.append(corner, f"dim on {input_background}")
        line.append(" " * right, f"not dim on {input_background}")
    return line


def _control_bar_lines(
    message: str,
    *,
    accent: str,
    input_background: str,
    width: int | None = None,
    corner: str = "",
    accent_column: bool = True,
    accent_mark: bool = False,
) -> list[RenderableType]:
    output_width = width or terminal_width()
    content_width = max(1, output_width - 4)
    body = Text(message)
    body.expand_tabs()
    wrapped_lines = [
        wrapped_line
        for line in body.plain.splitlines() or [""]
        for wrapped_line in wrap_display(line, content_width)
    ]
    lines: list[RenderableType] = [
        _control_bar_line(
            line,
            accent=accent,
            input_background=input_background,
            width=output_width,
            accent_head=accent_column or (accent_mark and index == 0),
        )
        for index, line in enumerate(wrapped_lines)
    ]
    return [
        _control_bar_line(
            accent=accent,
            input_background=input_background,
            width=output_width,
            accent_head=accent_column,
        ),
        *lines,
        _control_bar_line(
            accent=accent,
            input_background=input_background,
            width=output_width,
            corner=corner,
            accent_head=accent_column,
        ),
    ]


def _run_context(runnable: str, model: str, reasoning: str, width: int) -> str:
    """Fit snapshot fields, shortening the runnable before the model."""

    fields = [
        display_runnable_ref(runnable.rsplit("::", 1)[-1], surface="chat"),
        model,
        *([reasoning] if reasoning else []),
    ]
    widths = [display_width(value) for value in fields]
    overflow = max(0, sum(widths) + 3 * (len(fields) - 1) - width)
    for index, size in enumerate(widths):
        reduction = min(max(0, size - 1), overflow)
        widths[index] -= reduction
        overflow -= reduction
    return truncate(" · ".join(truncate(v, w) for v, w in zip(fields, widths)), width)


def _slash_control_lines(
    message: str,
    *,
    input_background: str,
    width: int | None = None,
) -> list[RenderableType]:
    return [
        *_control_bar_lines(
            message,
            accent=QUICK_COMMAND_CONTROL_ACCENT,
            input_background=input_background,
            width=width,
            accent_column=False,
            accent_mark=True,
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
    code_background: str = DARK_TERMINAL_SURFACES.code_background
    inline_code_background: str = DARK_TERMINAL_SURFACES.inline_code_background

    def update(self, event: Any) -> None:
        if isinstance(event, ProgressBlock):
            self.progress = event

    def render(self) -> RenderableType:
        rendered = progress_block_renderable(
            self.progress,
            live=self.live,
            max_width=self.max_width,
            code_background=self.code_background,
            inline_code_background=self.inline_code_background,
            code_foreground=None,
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
    input_background: str = DARK_TERMINAL_SURFACES.input_background
    runnable: str = ""
    model: str = ""
    reasoning: str = ""

    @classmethod
    def create(
        cls,
        message: str,
        *,
        input_background: str = DARK_TERMINAL_SURFACES.input_background,
        request: RunRequest | None = None,
    ) -> RunControlBlock:
        return cls(
            message=message,
            input_background=input_background,
            runnable=request.runnable.ref if request else "",
            model=(request.model.ref if request.model else "model unspecified")
            if request
            else "",
            reasoning=(model_reasoning_value(request.model) or "auto")
            if request and request.model
            else "",
        )

    def update(self, event: RunEvent) -> None:
        if isinstance(event, RunBegin) and event.parent is None and event.runnable:
            self.runnable = event.runnable

    def render(self) -> RenderableType:
        return self

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        width = max(1, options.max_width)
        yield from console.render(
            Group(
                *_control_bar_lines(
                    self.message,
                    accent=RUN_CONTROL_ACCENT,
                    input_background=self.input_background,
                    width=width,
                    corner=_run_context(
                        self.runnable, self.model, self.reasoning, max(1, width - 4)
                    )
                    if self.model
                    else "",
                    accent_column=False,
                    accent_mark=True,
                ),
                Text(),
            ),
            options,
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
class SteerFeedbackBlock(MutableBlock):
    """One replaceable aggregate explanation below pending steer bars."""

    accepted: int = 0
    sending: int = 0
    active_step: bool = False
    max_width: int = DEFAULT_MAX_PROGRESS_WIDTH
    vertical_padding: bool = True

    @property
    def message(self) -> str:
        count = self.accepted + self.sending
        return f"{count} steer{'s' if count != 1 else ''} pending"

    def render(self) -> RenderableType:
        return self

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        width = max(1, min(options.max_width, self.max_width))
        lines = wrap_display(self.message, max(1, width - 4))
        body = "\n".join(
            ("• " if index == 0 else "  ") + line for index, line in enumerate(lines)
        )
        yield Text(f"\n{body}\n\n" if self.vertical_padding else body, style="dim")


@dataclass(slots=True)
class RunSteerBlock(MutableBlock):
    """An independent steer bar, committed on matched adoption or Run end."""

    message: str
    run_id: str = ""
    max_width: int = DEFAULT_MAX_PROGRESS_WIDTH
    input_background: str = DARK_TERMINAL_SURFACES.input_background
    not_applied: bool = False

    @classmethod
    def create(
        cls,
        *,
        message: str,
        run_id: str,
        max_width: int = DEFAULT_MAX_PROGRESS_WIDTH,
        input_background: str = DARK_TERMINAL_SURFACES.input_background,
    ) -> RunSteerBlock:
        return cls(
            message=message,
            run_id=run_id,
            max_width=max_width,
            input_background=input_background,
        )

    def update(self, event: StepBegin | RunEnd) -> None:
        del event

    def render(self) -> RenderableType:
        return self

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        width = max(1, min(options.max_width, self.max_width))
        yield from console.render(
            Group(
                Text(),
                *_control_bar_lines(
                    self.message,
                    accent=STEER_CONTROL_ACCENT,
                    input_background=self.input_background,
                    width=width,
                    corner="not applied" if self.not_applied else "",
                    accent_column=False,
                    accent_mark=True,
                ),
                Text(),
            ),
            options.update_width(width),
        )


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
    code_background: str = DARK_TERMINAL_SURFACES.code_background
    inline_code_background: str = DARK_TERMINAL_SURFACES.inline_code_background

    @classmethod
    def create(
        cls,
        event: StepEnd,
        *,
        code_background: str = DARK_TERMINAL_SURFACES.code_background,
        inline_code_background: str = DARK_TERMINAL_SURFACES.inline_code_background,
    ) -> AssistantResponseBlock:
        return cls(
            text=parts_response_text(output_parts(event)),
            shape=shape_label(event),
            code_background=code_background,
            inline_code_background=inline_code_background,
        )

    @classmethod
    def from_parts(
        cls,
        parts: Sequence[Part],
        *,
        max_width: int = DEFAULT_MAX_PROGRESS_WIDTH,
        code_background: str = DARK_TERMINAL_SURFACES.code_background,
        inline_code_background: str = DARK_TERMINAL_SURFACES.inline_code_background,
    ) -> AssistantResponseBlock:
        return cls(
            text=parts_response_text(parts),
            max_width=max_width,
            code_background=code_background,
            inline_code_background=inline_code_background,
        )

    def update(self, event: Any) -> None:
        del event

    def render(self) -> RenderableType | None:
        if self.text:
            return response_renderable(
                self.text,
                max_width=self.max_width,
                code_background=self.code_background,
                inline_code_background=self.inline_code_background,
                code_foreground=None,
            )
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
    input_background: str = DARK_TERMINAL_SURFACES.input_background
    code_background: str = DARK_TERMINAL_SURFACES.code_background
    inline_code_background: str = DARK_TERMINAL_SURFACES.inline_code_background

    def render(self) -> RenderableType:
        return self

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        width = max(1, min(options.max_width, self.max_width))
        response = AssistantResponseBlock.from_parts(
            self.parts,
            max_width=self.max_width,
            code_background=self.code_background,
            inline_code_background=self.inline_code_background,
        ).render()
        lines = [
            *_slash_control_lines(
                self.message,
                input_background=self.input_background,
                width=width,
            ),
            _SlashResultDivider(self.run_id, max_width=self.max_width),
        ]
        if response is not None:
            lines.extend([Text(), response])
        yield from console.render(Group(*lines), options.update_width(width))


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
        caption = f"{self.run_id} output"
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
    executor_metadata: ChatExecutorMetadata
    client_version: str

    def render(self) -> RenderableType:
        return Group(Text(), self, Text("\n\n"))

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        runtime_value = _header_runtime_value(self.executor_metadata)
        sandbox_value = _header_sandbox_value(self.executor_metadata)
        workspaces = self.executor_metadata.workspaces
        workspace_value = (
            "unavailable" if workspaces is None else ", ".join(workspaces) or "none"
        )
        fields = (
            ("runtime", runtime_value),
            ("sandbox", sandbox_value),
            ("workspaces", Text(workspace_value)),
        )
        yield Banner(f"Chat {version_label(self.client_version)}", fields)


def _header_runtime_value(metadata: ChatExecutorMetadata) -> Text:
    if metadata.endpoint is None:
        return Text("embedded")
    if metadata.version is None:
        raise ValueError("remote chat executor metadata is missing its version")
    version = metadata.version
    return Text(version_label(version))


def _header_sandbox_value(metadata: ChatExecutorMetadata) -> Text:
    sandbox = Text(metadata.sandbox_driver)
    sandbox.append(" · ", style="dim")
    sandbox.append(metadata.sandbox_detail)
    return sandbox


@dataclass(frozen=True, slots=True)
class SlashBlock:
    message: str
    body: Sequence[str]
    kind: Literal["success", "result", "usage", "error"] = "result"
    max_width: int = DEFAULT_MAX_PROGRESS_WIDTH
    input_background: str = DARK_TERMINAL_SURFACES.input_background

    def render(self) -> RenderableType:
        return self

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        width = max(1, min(options.max_width, self.max_width))
        lines = _slash_control_lines(
            self.message,
            input_background=self.input_background,
            width=width,
        )
        for index, line in enumerate(self.body):
            styled = self._summary_line(line) if index == 0 else self._body_line(line)
            lines.extend(_wrap_slash_text(console, styled, width=width))
        yield from console.render(Group(*lines), options.update_width(width))

    def _summary_line(self, line: str) -> Text:
        if line.lstrip().startswith(("/", ":")):
            return self._command_line(line)
        if self._is_heading(line):
            return self._heading_line(line)
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
        if line.lstrip().startswith(("/", ":")):
            return SlashBlock._command_line(line)
        if SlashBlock._is_heading(line):
            return SlashBlock._heading_line(line)
        return Text.from_markup(f"[none]  {escape(line)}[/]")

    @staticmethod
    def _command_line(line: str) -> Text:
        stripped = line.lstrip()
        leading = line[: len(line) - len(stripped)]
        aligned_argument = re.fullmatch(
            r"(?P<command>[/:]\S+)(?P<gap>\s{2,})(?P<argument>[A-Z][A-Z0-9_.=-]*)",
            stripped,
        )
        if aligned_argument is not None:
            text = Text(f"  {leading}")
            text.append(aligned_argument.group("command"), style="cyan")
            text.append(aligned_argument.group("gap"))
            text.append(aligned_argument.group("argument"), style="dim")
            return text
        usage, _, summary = stripped.partition("  ")
        while summary.startswith(" "):
            summary = summary[1:]
        text = Text(f"  {leading}")
        SlashBlock._append_usage(text, usage)
        if summary:
            text.append(" " * (len(stripped) - len(usage) - len(summary)))
            text.append(summary, style="none")
        return text

    @staticmethod
    def _is_heading(line: str) -> bool:
        return line.strip() in {
            "Examples:",
            "Fields:",
            "Available overrides:",
            "Available shortcuts:",
            "Parameters:",
            "Values:",
            "Shortcuts:",
            "Session",
            "Inspection",
            "Other",
            "Overrides",
            "Model",
            "Runnable",
            "Fields",
            "Example",
            "Input focused:",
            "Queue focused:",
            "Global:",
        }

    @staticmethod
    def _heading_line(line: str) -> Text:
        return Text(f"  {line}", style="bold")

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
    max_width: int = DEFAULT_MAX_PROGRESS_WIDTH
    input_background: str = DARK_TERMINAL_SURFACES.input_background

    def render(self) -> RenderableType:
        return self

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        width = max(1, min(options.max_width, self.max_width))
        lines: list[RenderableType] = _slash_control_lines(
            self.message,
            input_background=self.input_background,
            width=width,
        )
        lines.extend(
            _wrap_slash_text(console, Text(f"  {self.table.summary}"), width=width)
        )
        if self.table.rows:
            lines.extend((Text(), _SlashTableRows(self.table)))
        yield from console.render(Group(*lines), options.update_width(width))


@dataclass(frozen=True, slots=True)
class SlashHelpBlock:
    """Render the structured main slash help."""

    message: str
    help: SlashHelp
    max_width: int = DEFAULT_MAX_PROGRESS_WIDTH
    input_background: str = DARK_TERMINAL_SURFACES.input_background

    def render(self) -> RenderableType:
        return self

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        width = max(1, min(options.max_width, self.max_width))
        inset = min(2, max(0, width - 1))
        lines: list[RenderableType] = _slash_control_lines(
            self.message,
            input_background=self.input_background,
            width=width,
        )
        for line in outcome_lines(
            SlashOutcome("result", self.help), width=max(1, width - inset)
        ):
            styled = SlashBlock._body_line(line)
            # Alias notes stay visually subordinate even after wrapping.
            if "alias:" in styled.plain:
                start = styled.plain.index("alias:")
                styled.stylize("dim", max(0, start - 1))
            lines.extend(_wrap_slash_text(console, styled, width=width))
        yield from console.render(Group(*lines), options.update_width(width))


def _wrap_slash_text(console: Console, text: Text, *, width: int) -> list[Text]:
    """Preserve each logical line's inset on every wrapped physical line."""
    if not text.plain.strip():
        return [Text()]
    leading = len(text.plain) - len(text.plain.lstrip(" "))
    indent = min(leading, max(0, width - 1))
    content = text[leading:]
    return [
        Text(" " * indent) + part
        for part in content.wrap(console, max(1, width - indent), overflow="fold")
    ]


@dataclass(frozen=True, slots=True)
class _SlashTableRows:
    table: SlashTable

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        del console
        indent = 2 if options.max_width >= 4 else 0
        lines = table_lines(
            self.table.headers,
            self.table.rows,
            width=max(1, options.max_width - indent),
            shrink_order=self.table.shrink_order,
            protected_suffixes=self.table.protected_suffixes,
        )
        for index, line in enumerate(lines):
            rendered = Text(" " * indent)
            rendered.append(line, style="dim" if index == 1 else "none")
            rendered.no_wrap = True
            yield rendered
