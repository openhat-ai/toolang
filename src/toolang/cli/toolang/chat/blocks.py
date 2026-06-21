"""Mutable blocks for the chat TUI."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from rich import box
from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from toolang.base.types.message import (
    Message,
    Part,
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    message_text,
)
from toolang.execution.events import (
    PartBegin,
    PartDelta,
    PartEnd,
    RunBegin,
    RunEnd,
    RunStarting,
    RunSteering,
    RunStopping,
    RunWaiting,
    StepBegin,
    StepEnd,
    TraceEvent,
)

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

    @classmethod
    def create(cls, event: Any) -> "MutableBlock":
        raise NotImplementedError

    def update(self, event: Any) -> None:
        raise NotImplementedError

    def render(self) -> RenderableType | None:
        raise NotImplementedError


@dataclass(slots=True)
class GenericCommandBlock(MutableBlock):
    """Fallback command block for command kinds without a dedicated UI yet."""

    index: int
    command_kind: str

    @classmethod
    def create(cls, event: TraceEvent) -> "GenericCommandBlock":
        return cls(
            index=int(getattr(event, "index", 0) or 0),
            command_kind=event.type.removeprefix("run_") or "command",
        )

    def update(self, event: TraceEvent) -> None:
        if kind := event.type.removeprefix("run_"):
            self.command_kind = kind

    def render(self) -> RenderableType:
        return Text.from_markup(
            f"[dim]{escape(progress_tail(f'· {self.command_kind}'))}[/]"
        )


@dataclass(slots=True)
class RunStartBlock(MutableBlock):
    """Created by run_starting/run_waiting and moved by run_begin/run_end."""

    message: str
    run_id: str = ""
    waiting_reason: str = ""
    waiting_position: int | None = None
    waiting: bool = False

    @classmethod
    def create(cls, event: RunStarting | RunWaiting) -> "RunStartBlock":
        if event.type == "run_starting":
            return cls(
                message=_message_text(cast(RunStarting, event).input),
                run_id=event.run_id,
            )
        waiting = cast(RunWaiting, event)
        return cls(
            message="",
            run_id=waiting.run_id,
            waiting_reason=waiting.reason,
            waiting_position=waiting.position,
            waiting=True,
        )

    def update(self, event: RunStarting | RunWaiting | RunBegin | RunEnd) -> None:
        if event.type == "run_starting":
            starting = cast(RunStarting, event)
            if message := _message_text(starting.input):
                self.message = message
            self.run_id = starting.run_id or self.run_id
            self.waiting = False
            self.waiting_reason = ""
            self.waiting_position = None
        elif event.type == "run_waiting":
            waiting = cast(RunWaiting, event)
            self.run_id = waiting.run_id or self.run_id
            self.waiting = True
            self.waiting_reason = waiting.reason
            self.waiting_position = waiting.position
        elif event.type == "run_begin":
            begin = cast(RunBegin, event)
            if message := _message_text(begin.input):
                self.message = message
            self.run_id = begin.run_id or self.run_id
            self.waiting = False
            self.waiting_reason = ""
            self.waiting_position = None
        elif event.type == "run_end":
            self.run_id = event.run_id or self.run_id
            self.waiting = False
            self.waiting_reason = ""
            self.waiting_position = None

    def _footer(self) -> str:
        if self.waiting_reason or self.waiting_position:
            suffix = (
                f" · position {self.waiting_position}" if self.waiting_position else ""
            )
            run_id = f" {self.run_id}" if self.run_id else ""
            return f"  waiting{run_id} for {self.waiting_reason or 'queue'}{suffix}"
        if self.run_id:
            return f"  {self.run_id}"
        if self.waiting:
            return "  waiting"
        return ""

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
    """Created by run_steering and moved by the next step_begin or run_end."""

    index: int
    message: str
    run_id: str = ""
    pending: bool = True

    @classmethod
    def create(cls, event: RunSteering) -> "RunSteerBlock":
        return cls(
            index=event.index,
            message=_message_text(event.message),
            run_id=event.run_id,
        )

    def update(self, event: RunSteering | StepBegin | RunEnd) -> None:
        if event.type == "run_steering" and (
            message := _message_text(cast(RunSteering, event).message)
        ):
            self.message = message
        self.pending = event.type == "run_steering"
        if run_id := event.run_id:
            self.run_id = run_id

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
    def create(cls, event: RunBegin | RunStopping | RunEnd) -> "RunStopBlock":
        if event.type == "run_begin":
            return cls(run_id=event.run_id or "run", status="running")
        if event.type == "run_stopping":
            return cls(run_id=event.run_id or "run", status="canceling")
        run_end = cast(RunEnd, event)
        return cls(
            run_id=run_end.run_id or "run",
            status=cls._display_status(run_end.status),
            error=friendly_error(run_end.error) if run_end.error else "",
        )

    def update(self, event: RunBegin | RunStopping | RunEnd) -> None:
        self.run_id = event.run_id or self.run_id
        if event.type == "run_begin":
            self.status = "running"
            self.error = ""
            return
        if event.type == "run_stopping":
            self.status = "canceling"
            self.error = ""
            return
        run_end = cast(RunEnd, event)
        self.status = self._display_status(run_end.status)
        self.error = friendly_error(run_end.error) if run_end.error else self.error

    def render(self) -> RenderableType:
        run_id = self.run_id
        status = self.status
        error = self.error

        if status in {
            "",
            "queued",
            "waiting",
            "submitting",
            "running",
            "succeeded",
            "finished",
            "completed",
            "done",
        }:
            return Text("\n")

        if status == "canceling":
            return Text.from_markup("[dim]canceling...[/]")

        if status in {"canceled", "cancelled"}:
            return Group(
                Text.from_markup(
                    f"[yellow]  -------- {escape(run_id)} canceled --------[/]"
                ),
                Text("\n"),
            )

        if status in {"failed", "error"}:
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
    def _display_status(status: str) -> str:
        return "succeeded" if status == "finished" else status.strip().lower()

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

    index: int
    step_kind: str
    label: str = ""
    status: str = "running"
    final_label: str = ""
    error: str = ""
    part_deltas: dict[int, list[str]] = field(default_factory=dict)

    @classmethod
    def create(cls, event: StepBegin) -> "DefaultStepBlock":
        step_kind = event.kind
        payload = event.metadata
        return cls(
            index=event.step_index,
            step_kind=step_kind,
            label=cls._initial_label(step_kind, payload),
        )

    @staticmethod
    def _initial_label(step_kind: str, payload: Mapping[str, Any]) -> str:
        if step_kind == "run":
            target_kind = as_text(payload.get("target_kind")) or "run"
            target = as_text(payload.get("target"))
            return (
                f"running {target_kind} {target}"
                if target
                else f"running {target_kind}"
            )
        if step_kind in {"step", "parallel", "bind"}:
            return f"running {as_text(payload.get('op')) or 'flow'}"
        if step_kind == "system":
            return (
                as_text(payload.get("message"))
                or as_text(payload.get("op"))
                or step_kind
            )
        return "running"

    def update(
        self, event: StepBegin | PartBegin | PartDelta | PartEnd | StepEnd
    ) -> None:
        if event.type == "part_delta":
            part_delta = cast(PartDelta, event)
            delta = part_delta.delta
            if not isinstance(delta, TextDelta):
                return
            if delta.text:
                self.part_deltas.setdefault(part_delta.part_index, []).append(
                    delta.text
                )
            return
        if event.type == "step_end":
            step_end = cast(StepEnd, event)
            self.status = "completed"
            payload = step_end.payload.to_data()
            self.error = step_end.error or ""
            self.final_label = self._final_label(payload)

    def _text_delta(self) -> str:
        return "".join(
            chunk
            for part_index in sorted(self.part_deltas)
            for chunk in self.part_deltas[part_index]
        )

    def render(self) -> RenderableType:
        kind = self.step_kind
        marker = self._marker()
        running = self.status != "completed"
        final_label = self.final_label or self.label.removeprefix("running ")

        if running:
            text_delta = self._text_delta()
            if kind == "model" and text_delta:
                preview = summarize(" ".join(text_delta.split()), width=120)
                return Text.from_markup(
                    f"[cyan]{escape(progress_tail(f'{marker} {preview}'))}[/]"
                )
            line = progress_tail(f"{marker} {self.label}")
            style = "cyan" if kind == "model" else "dim"
            return Text.from_markup(f"[{style}]{escape(line)}[/]")

        if kind == "run":
            return Text.from_markup(f"[dim]{escape(f'{marker} ran {final_label}')}[/]")

        if kind in {"step", "parallel", "bind"}:
            return Text.from_markup(f"[dim]{escape(f'{marker} ran {final_label}')}[/]")

        if kind in {"system", "error"}:
            message = self.error or self.final_label or self.label or "runtime event"
            style = "red" if kind == "error" else "magenta"
            return Text.from_markup(f"[{style}]{escape(f'{marker} {message}')}[/]")

        label = kind or "step"
        return Text.from_markup(f"[dim]{escape(f'{marker} ran {label}')}[/]")

    def _marker(self) -> str:
        if self.step_kind == "model":
            return "•"
        if self.step_kind == "tool":
            return "›"
        if self.step_kind == "run":
            return "›"
        if self.step_kind == "step":
            return "-"
        if self.step_kind == "parallel":
            return "..."
        if self.step_kind == "bind":
            return "->"
        if self.step_kind == "system":
            return "◇"
        if self.step_kind == "error":
            return "!"
        return "·"

    def _final_label(self, payload: Mapping[str, Any]) -> str:
        if self.step_kind == "run":
            target_kind = as_text(payload.get("target_kind")) or "run"
            target_name = as_text(payload.get("target"))
            return f"{target_kind} {target_name}" if target_name else target_kind
        return (
            as_text(payload.get("message"))
            or as_text(payload.get("op"))
            or as_text(payload.get("status"))
            or self.label.removeprefix("running ")
        )


@dataclass(slots=True)
class ModelStepBlock(MutableBlock):
    """Model step block."""

    index: int
    status: str = "thinking"
    message: str = ""
    output: str = ""
    tool_requests: list[str] = field(default_factory=list)
    model: str = ""

    @classmethod
    def create(cls, event: StepBegin) -> "ModelStepBlock":
        payload = event.metadata
        return cls(
            index=event.step_index,
            model=as_text(payload.get("model_ref"))
            or as_text(payload.get("model"))
            or "",
        )

    def update(
        self, event: StepBegin | PartBegin | PartDelta | PartEnd | StepEnd
    ) -> None:
        if event.type == "part_delta":
            delta = cast(PartDelta, event).delta
            if isinstance(delta, TextDelta):
                self.message += delta.text
        elif event.type == "step_end":
            step_end = cast(StepEnd, event)
            self.status = "completed"
            self.output = _parts_text(step_end.output)
            self.tool_requests = self._tool_request_summary(step_end)
            payload = step_end.payload.to_data()
            self.model = (
                as_text(payload.get("model_ref"))
                or as_text(payload.get("model"))
                or self.model
            )

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

    index: int
    detail: str
    status: str = "running"
    error: str = ""
    output_messages: list[str] = field(default_factory=list)

    @classmethod
    def create(cls, event: StepBegin) -> "ToolStepBlock":
        return cls(index=event.step_index, detail="tool")

    def update(
        self, event: StepBegin | PartBegin | PartDelta | PartEnd | StepEnd
    ) -> None:
        if event.type != "step_end":
            return
        step_end = cast(StepEnd, event)
        self.status = "completed"
        self.detail = _tool_call_display_from_parts(step_end.output)
        self.error = step_end.error or ""
        self.output_messages = self._output_messages(step_end)

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


def _tool_call_display_from_parts(parts: Sequence[Part]) -> str:
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


def _message_text(message: Message) -> str:
    return message_text(message.parts).strip()


def _parts_text(parts: Sequence[Part]) -> str:
    return "".join(part.text for part in parts if isinstance(part, TextPart)).strip()
