"""Mutable blocks for terminal chat."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from rich import box
from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from toolang.base.types.message import (
    MessagePart,
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.execution.events import (
    PartDelta,
    RunBegin,
    RunEnd,
    RunEvent,
    StepBegin,
    StepEnd,
)
from toolang.execution.types import StepPath

from .base import as_text, friendly_error
from .rendering import (
    bar,
    display_len,
    markdown_width,
    progress_tail,
    render_segments,
    summarize,
    terminal_width,
    truncate_display,
)

STEER_BAR_BG = "#2f555d"


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
class RunStartBlock(MutableBlock):
    """Created by a local submission and finalized by run_begin/run_end."""

    message: str
    run_id: str = ""

    @classmethod
    def create(cls, message: str) -> "RunStartBlock":
        return cls(message=message)

    def update(self, event: RunEvent) -> None:
        if isinstance(event, (RunBegin, RunEnd)):
            self.run_id = event.run or self.run_id

    def _footer(self) -> str:
        if self.run_id:
            return f"  {self.run_id}"
        return "  starting"

    def render(self) -> RenderableType:
        footer = self._footer()
        lines: list[RenderableType] = [bar([], style="white on grey23")]
        for index, line in enumerate(self.message.splitlines() or [""]):
            lines.append(
                bar([(">", "grey70 on grey23"), (f" {line}", "white on grey23")])
                if index == 0
                else bar([(f"  {line}", "white on grey23")], style="white on grey23")
            )
        lines.append(bar([(footer, "grey70 on grey23")], style="white on grey23"))
        lines.append(Text("\n"))
        return Group(*lines)


@dataclass(slots=True)
class RunSteerBlock(MutableBlock):
    """Created by a local steer and moved by the next step_begin or run_end."""

    message: str
    run_id: str = ""
    pending: bool = True

    @classmethod
    def create(cls, *, message: str, run_id: str) -> "RunSteerBlock":
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
    """Created by run_begin and finalized by run_end."""

    run_id: str
    status: str
    error: str = ""

    @classmethod
    def create(cls, event: RunBegin | RunEnd) -> "RunStopBlock":
        if isinstance(event, RunBegin):
            return cls(run_id=event.run or "run", status="running")
        run_end = event
        return cls(
            run_id=run_end.run or "run",
            status=run_end.status,
            error=friendly_error(run_end.error) if run_end.error else "",
        )

    def update(self, event: RunBegin | RunEnd) -> None:
        self.run_id = event.run or self.run_id
        if isinstance(event, RunBegin):
            self.status = "running"
            self.error = ""
            return
        run_end = event
        self.status = run_end.status
        self.error = friendly_error(run_end.error) if run_end.error else self.error

    def mark_canceling(self) -> None:
        self.status = "canceling"
        self.error = ""

    def render(self) -> RenderableType:
        run_id = self.run_id
        status = self.status
        error = self.error

        if status in {"running", "finished"}:
            return Text("\n")

        if status == "canceling":
            return Text.from_markup("[dim]canceling...[/]")

        if status == "canceled":
            return Group(
                Text.from_markup(
                    f"[yellow]  -------- {escape(run_id)} canceled --------[/]"
                ),
                Text("\n"),
            )

        if status == "failed":
            lines: list[RenderableType] = [
                Text.from_markup(f"[red]  -------- {escape(run_id)} failed --------[/]")
            ]
            if error:
                lines.extend(
                    Text.from_markup(f"[red]  {escape(line)}[/]")
                    for line in self._wrap_plain_lines(error)
                )
            lines.append(Text("\n"))
            return Group(*lines)

        return Group(
            Text.from_markup(
                f"[dim]  -------- {escape(run_id)} {escape(status)} --------[/]"
            ),
            Text("\n"),
        )

    @staticmethod
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


@dataclass(slots=True)
class DefaultStepBlock(MutableBlock):
    """Fallback step block for step kinds that do not have a dedicated block yet."""

    step: StepPath
    step_kind: str
    label: str = ""
    status: str = "running"
    final_label: str = ""
    error: str = ""

    @classmethod
    def create(cls, event: StepBegin) -> "DefaultStepBlock":
        step_kind = event.kind
        payload = event.given
        return cls(
            step=event.step,
            step_kind=step_kind,
            label=cls._initial_label(step_kind, payload),
        )

    @staticmethod
    def _initial_label(step_kind: str, payload: Mapping[str, Any]) -> str:
        if step_kind == "system":
            return (
                as_text(payload.get("message"))
                or as_text(payload.get("statement"))
                or step_kind
            )
        return f"running {step_kind}"

    def update(self, event: StepEnd) -> None:
        self.step = event.step
        self.status = "completed"
        self.error = event.error or ""
        self.final_label = self._final_label(event.noted)

    def render(self) -> RenderableType:
        kind = self.step_kind
        marker = self._marker()
        running = self.status != "completed"

        if running:
            line = progress_tail(f"{marker} {self.label}")
            return Text.from_markup(f"[dim]{escape(line)}[/]")

        if kind == "system":
            message = self.error or self.final_label or self.label or "runtime event"
            return Text.from_markup(f"[magenta]{escape(f'{marker} {message}')}[/]")

        label = kind or "step"
        return Text.from_markup(f"[dim]{escape(f'{marker} ran {label}')}[/]")

    def _marker(self) -> str:
        if self.step_kind == "par":
            return "..."
        if self.step_kind == "system":
            return "◇"
        return "·"

    def _final_label(self, payload: Mapping[str, Any]) -> str:
        return (
            as_text(payload.get("message"))
            or as_text(payload.get("statement"))
            or self.label.removeprefix("running ")
        )


@dataclass(slots=True)
class FlowStepBlock(MutableBlock):
    """Flow operation step block."""

    step: StepPath
    step_kind: str
    summary: str
    status: str = "running"
    error: str = ""

    @classmethod
    def create(cls, event: StepBegin) -> "FlowStepBlock":
        summary = cls._summary(event.kind, event.given)
        return cls(
            step=event.step,
            step_kind=event.kind,
            summary=summary,
        )

    def update(self, event: StepEnd) -> None:
        self.step = event.step
        self.status = event.status
        self.error = event.error or ""
        self.summary = self._summary(event.kind, event.noted)

    def render(self) -> RenderableType:
        marker = self._marker()
        summary = self.summary or "flow step"

        if self.status == "running":
            return Text.from_markup(
                f"[dim]{escape(progress_tail(f'{marker} running {summary}'))}[/]"
            )

        if self.error or self.status == "failed":
            error = f": {summarize(self.error, width=120)}" if self.error else ""
            return Text.from_markup(
                f"[red]{escape(f'{marker} failed {summary}{error}')}[/]"
            )

        return Text.from_markup(f"[dim]{escape(f'{marker} ran {summary}')}[/]")

    def _marker(self) -> str:
        if self.step_kind == "par":
            return "..."
        if self.step_kind == "loop":
            return "-"
        return "·"

    @staticmethod
    def _fallback_summary(step_kind: str, payload: Mapping[str, Any]) -> str:
        return as_text(payload.get("statement")) or step_kind or "flow step"

    @classmethod
    def _summary(cls, step_kind: str, payload: Mapping[str, Any]) -> str:
        statement = cls._fallback_summary(step_kind, payload)
        target = (
            as_text(payload.get("runnable"))
            or as_text(payload.get("predicate"))
            or as_text(payload.get("scorer"))
            or as_text(payload.get("agent"))
        )
        count = payload.get("count")
        suffix = f" {count}" if isinstance(count, int) else ""
        return f"{statement}{suffix}{f' {target}' if target else ''}"


@dataclass(slots=True)
class ChildRunStepBlock(MutableBlock):
    """Child agic or flow call step block."""

    step: StepPath
    summary: str
    status: str = "running"
    error: str = ""

    @classmethod
    def create(cls, event: StepBegin) -> "ChildRunStepBlock":
        return cls(
            step=event.step,
            summary=cls._summary(event.given),
        )

    def update(self, event: StepEnd) -> None:
        self.step = event.step
        self.status = event.status
        self.error = event.error or ""
        self.summary = self._summary(event.noted)

    def render(self) -> RenderableType:
        summary = self.summary or "child run"

        if self.status == "running":
            return Text.from_markup(
                f"[dim]{escape(progress_tail(f'• running {summary}'))}[/]"
            )

        if self.error or self.status == "failed":
            error = f": {summarize(self.error, width=120)}" if self.error else ""
            return Text.from_markup(f"[red]{escape(f'• failed {summary}{error}')}[/]")

        return Text.from_markup(f"[dim]{escape(f'• ran {summary}')}[/]")

    @staticmethod
    def _summary(payload: Mapping[str, Any]) -> str:
        statement = as_text(payload.get("statement")) or "run"
        runnable = as_text(payload.get("runnable"))
        placement = payload.get("placement")
        item = placement.get("item") if isinstance(placement, Mapping) else None
        suffix = f" item {item + 1}" if isinstance(item, int) else ""
        return f"{statement}{f' {runnable}' if runnable else ''}{suffix}"


@dataclass(slots=True)
class ModelStepBlock(MutableBlock):
    """Model step block."""

    step: StepPath
    status: str = "thinking"
    message: str = ""
    output: str = ""
    tool_requests: list[str] = field(default_factory=list)
    model: str = ""

    @classmethod
    def create(cls, event: StepBegin) -> "ModelStepBlock":
        payload = event.given
        model = payload.get("model")
        model_data = model if isinstance(model, Mapping) else {}
        return cls(
            step=event.step,
            model=as_text(model_data.get("ref"))
            or as_text(model_data.get("model"))
            or "",
        )

    def update(self, event: PartDelta | StepEnd) -> None:
        self.step = event.step
        if isinstance(event, PartDelta):
            delta = event.delta
            if isinstance(delta, TextDelta):
                self.message += delta.text
        else:
            self.status = "completed"
            self.output = _parts_text(event.output)
            self.tool_requests = self._tool_request_summary(event)

    def render(self) -> RenderableType:
        running = self.status != "completed"
        message = self.message
        output = self.output.strip()
        requests = "; ".join(self.tool_requests)

        if running:
            if message:
                return self._render_markdown_output(
                    [progress_tail(" ".join(message.split()))]
                )
            return Text.from_markup("[cyan]•[/] [dim]thinking...[/]")

        if output:
            output_lines = output.splitlines() or [output]
            return self._render_markdown_output(output_lines)

        if requests:
            return Text.from_markup(
                f"[cyan]•[/] [none]{escape(f'requested {requests}')}[/]"
            )

        suffix = f" ({self.model})" if self.model else ""
        return Text.from_markup(
            f"[cyan]•[/] [dim]{escape(f'[no text message]{suffix}')}[/]"
        )

    @staticmethod
    def _render_markdown_output(lines: Sequence[str]) -> RenderableType:
        rows: list[list[tuple[str, Any]]] = [[]]
        width = max(20, markdown_width() - 2)
        for segment in render_segments(Markdown("\n".join(lines)), width=width):
            if segment.control or not segment.text:
                continue
            parts = segment.text.split("\n")
            for index, part in enumerate(parts):
                if index:
                    rows.append([])
                if part:
                    rows[-1].append((part, segment.style))

        while rows and not any(text.strip() for text, _style in rows[0]):
            rows.pop(0)
        while rows and not any(text.strip() for text, _style in rows[-1]):
            rows.pop()
        if not rows:
            return Text.from_markup("[cyan]•[/]")

        rendered_rows: list[Text] = []
        for index, row in enumerate(rows):
            line = Text()
            if index == 0:
                line.append("•", style="cyan")
                line.append(" ")
            else:
                line.append("  ")
            for text, style in row:
                line.append(text, style=style)
            rendered_rows.append(line)
        return Group(*rendered_rows)

    @staticmethod
    def _tool_request_summary(event: StepEnd) -> list[str]:
        tools: list[str] = []
        for part in event.output:
            if not isinstance(part, ToolCallPart):
                continue
            tools.append(
                _tool_call_display(
                    part.tool_name or part.tool_family or "tool", dict(part.input)
                )
            )
        return tools


@dataclass(slots=True)
class ToolStepBlock(MutableBlock):
    """Tool step block."""

    step: StepPath
    detail: str
    status: str = "running"
    error: str = ""
    output_messages: list[str] = field(default_factory=list)

    @classmethod
    def create(cls, event: StepBegin) -> "ToolStepBlock":
        return cls(
            step=event.step,
            detail="tool",
        )

    def update(self, event: StepEnd) -> None:
        self.step = event.step
        self.status = "completed"
        self.detail = _tool_call_display_from_parts(event.output)
        self.error = event.error or ""
        self.output_messages = self._output_messages(event)

    def render(self) -> RenderableType:
        detail = self.detail
        output = " ".join("\n".join(self.output_messages).split())

        if self.status == "running":
            return Text.from_markup(
                f"[dim]•[/] [dim]{escape(progress_tail(f'running {detail}'))}[/]"
            )

        if self.error:
            message = f"ran {detail} failed: {summarize(self.error, width=120)}"
            line = Text.from_markup(f"[dim]•[/] [dim]{escape(message)}[/]")
            if output:
                width = max(8, markdown_width() - 2)
                output = truncate_display(output, width=width)
                return Group(line, Text.from_markup(f"[dim]  {escape(output)}[/]"))
            return line

        line = Text.from_markup(f"[dim]•[/] [dim]{escape(f'ran {detail}')}[/]")
        if not output:
            return line

        width = max(8, markdown_width() - 2)
        output = truncate_display(output, width=width)
        return Group(line, Text.from_markup(f"[dim]  {escape(output)}[/]"))

    @staticmethod
    def _output_messages(event: StepEnd) -> list[str]:
        messages: list[str] = []
        for part in event.output:
            if not isinstance(part, ToolResultPart):
                continue
            stdout = as_text(part.output.get("stdout"))
            stderr = as_text(part.output.get("stderr"))
            if stdout:
                messages.append(stdout)
            if stderr:
                messages.append(stderr)
            if not stdout and not stderr and part.output:
                messages.append(_plain_value(part.output))
        return messages


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
            lines.append(Text.from_markup(f"[dim]:[/] [bold]{escape(first)}[/]"))
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
        if line.startswith("/"):
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
            pad = max(2, 34 - text.cell_len)
            text.append(" " * pad)
            text.append(summary, style="none")
        return text

    @staticmethod
    def _table_line(columns: Sequence[str]) -> Text:
        text = Text("  ")
        first, *rest = columns
        badge = rest[0] if rest and rest[0] in {"current", "default"} else ""
        details = rest[1:] if badge else rest
        text.append(first, style="cyan")
        first_width = 40
        text.append(" " * max(2, first_width - display_len(first)))
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
            style = "cyan" if token.startswith("/") else "dim"
            if token.startswith("/"):
                command, separator, rest = token.partition(",")
                text.append(command, style=style)
                if separator:
                    text.append(separator, style="dim")
                    text.append(rest, style="cyan" if rest.startswith("/") else "dim")
            else:
                text.append(token, style=style)


def _split_columns(line: str) -> list[str]:
    return [part for part in re.split(r" {2,}", line.strip()) if part]


def _plain_value(value: object) -> str:
    if isinstance(value, str):
        return summarize(value, width=160)
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    return summarize(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")), width=160
    )


def _tool_call_display_from_parts(parts: Sequence[MessagePart]) -> str:
    for part in parts:
        if isinstance(part, ToolCallPart):
            return _tool_call_display(
                part.tool_name or part.tool_family or "tool",
                dict(part.input),
            )
        if isinstance(part, ToolResultPart):
            return part.tool_name or part.tool_family or "tool"
    return "tool"


def _tool_call_display(name: str, tool_input: dict[str, Any]) -> str:
    if not tool_input:
        return name
    for key in ("command", "cmd", "query", "path", "url", "prompt", "text"):
        if (value := tool_input.get(key)) is not None:
            return f"{name}: {_plain_value(value)}"
    if len(tool_input) == 1:
        return f"{name}: {_plain_value(next(iter(tool_input.values())))}"
    summary = ", ".join(
        f"{key}={_plain_value(value)}" for key, value in tool_input.items()
    )
    return f"{name}: {summary}"


def _parts_text(parts: Sequence[MessagePart]) -> str:
    return "".join(part.text for part in parts if isinstance(part, TextPart)).strip()
