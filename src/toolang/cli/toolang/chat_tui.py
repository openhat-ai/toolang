"""Terminal chat TUI state, layout, and rendering helpers."""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import io
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import tomllib
from typing import Any, Literal, cast
from uuid import uuid4

import click
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from rich.markdown import Markdown
import typer

from ...execution.labels import executable_label
from ...execution.projection import (
    FlowCallView,
    FlowStageView,
    project_flow_from_run,
    project_flow_from_step_payloads,
    stage_calls,
    stage_lanes,
    stage_title_label,
)
from ...models.resolution import split_model_selectors
from ..chat_history import ChatInputHistoryStore


@dataclass(frozen=True, slots=True)
class ChatTuiDependencies:
    runtime_json: Callable[[typer.Context, str], dict[str, Any]]
    runtime_post: Callable[..., dict[str, Any]]
    runtime_consume_stream: Callable[..., None]
    message_payload: Callable[[str], dict[str, object]]
    input_history_store: Callable[[typer.Context], ChatInputHistoryStore | None]
    home_label: Callable[[typer.Context], str]
    write_lines: Callable[..., None]


_CHAT_MAX_INPUT_ROWS = 6
_CHAT_MAX_QUEUE_ROWS = 4
_CHAT_DIM = "\x1b[2m"
_CHAT_NORMAL_INTENSITY = "\x1b[22m"
_CHAT_RESET = "\x1b[0m"
_CHAT_BOLD = "\x1b[1m"
_CHAT_QUEUE_FG = "#f2f2f2"
_CHAT_QUEUE_BG = "#3a3a3a"
_CHAT_QUEUE_DIM_FG = "#b8b8b8"
_CHAT_INPUT_FG = "#f5f5f5"
_CHAT_INPUT_BG = "#444444"
_CHAT_INPUT_DIM_FG = "#b8b8b8"
_CHAT_STEER_INPUT_FG = "#f5f5f5"
_CHAT_STEER_INPUT_BG = "#2f555d"
_CHAT_STEER_INPUT_DIM_FG = "#b8b8b8"
_CHAT_STATUS_FG = "#f2f2f2"
_CHAT_STATUS_BG = "#5a5a5a"
_CHAT_CURSOR_FG = "#111111"
_CHAT_CURSOR_BG = "#eeeeee"
_CHAT_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_CHAT_FLOW_DETAIL_INDENT = "  "
_CHAT_FLOW_STATEMENT_MARKER = "‣"


@dataclass(frozen=True)
class _ChatInputBarSegment:
    text: str
    dim: bool = False


@dataclass(frozen=True)
class _ChatInputBarRow:
    segments: tuple[_ChatInputBarSegment, ...] = ()
    bar: bool = True


@dataclass(frozen=True)
class _ChatInputBarSpec:
    kind: Literal["normal", "steer"]
    marker: str
    text: str
    footer: str = ""
    footer_dim: bool = False
    outer_blank: bool = False


def _chat_fixed_height(rows: int, *, minimum: int) -> Dimension:
    height = max(minimum, rows)
    return Dimension(min=minimum, preferred=height, max=height, weight=0)


@dataclass(frozen=True, slots=True)
class _ChatUIEvent:
    type: str
    value: str | dict[str, Any] | None = None


@dataclass(slots=True)
class _ChatMutableBlock:
    index: int
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    finalized: bool = False

    @classmethod
    def create(cls, payload: Mapping[str, Any], *, run: "_ChatRun | None" = None) -> "_ChatMutableBlock":
        del run
        index = _chat_block_index(payload)
        kind = str(payload.get("kind") or "unknown")
        return cls(index=index, kind=kind, payload=dict(payload))

    def update(self, payload: Mapping[str, Any]) -> None:
        self.payload.update(dict(payload))

    def delta(self, payload: Mapping[str, Any]) -> None:
        self.update(payload)

    def finalize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.update(payload)
        self.finalized = True
        return dict(self.payload)


@dataclass(slots=True)
class _ChatCommandBlock(_ChatMutableBlock):
    @classmethod
    def create(cls, payload: Mapping[str, Any], *, run: "_ChatRun | None" = None) -> "_ChatCommandBlock":
        del run
        index = _chat_command_index(payload)
        return cls(
            index=index,
            kind=str(payload.get("kind") or "command"),
            payload=dict(payload),
        )


@dataclass(slots=True)
class _ChatStep(_ChatMutableBlock):
    label: str = ""
    frame: int = 0
    part_deltas: dict[int, list[str]] = field(default_factory=dict)

    @classmethod
    def create(cls, payload: Mapping[str, Any], *, run: "_ChatRun | None" = None) -> "_ChatStep":
        index = _chat_step_index(payload)
        stored_payload = dict(payload)
        if stored_payload.get("kind") in {"step", "parallel", "bind", "run"} and "payload" not in stored_payload:
            stored_payload["payload"] = dict(_mapping(stored_payload.get("metadata")))
        kind = str(stored_payload.get("kind") or "unknown")
        return cls(
            index=index,
            kind=kind,
            payload=stored_payload,
            label=_chat_step_label(stored_payload, run),
        )

    def delta(self, payload: Mapping[str, Any]) -> None:
        _ChatMutableBlock.delta(self, payload)
        delta = _mapping(payload.get("delta"))
        if delta.get("type") != "text":
            return
        text = _text(delta.get("text"))
        if not text:
            return
        part_index = _chat_part_index(payload)
        self.part_deltas.setdefault(part_index, []).append(text)

    def finalize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        completed_payload = dict(payload)
        if "input" not in completed_payload and "input" in self.payload:
            completed_payload["input"] = self.payload["input"]
        self.update(completed_payload)
        self.finalized = True
        return completed_payload

    def text_delta(self) -> str:
        return "".join(
            chunk
            for part_index in sorted(self.part_deltas)
            for chunk in self.part_deltas[part_index]
        )


@dataclass(frozen=True, slots=True)
class _ChatToolCall:
    name: str
    input: dict[str, Any]


@dataclass(slots=True)
class _ChatQueueItem:
    kind: Literal["run", "steer"]
    text: str


@dataclass(slots=True)
class _ChatRun:
    run_id: str
    message: str
    status: str
    executable_kind: str = "thunk"
    executable_name: str | None = None
    accept_child_trace: bool = False
    queue_state: str | None = None
    waiting_reason: str | None = None
    queue_position: int | None = None
    cancel_requested: bool = False
    cancel_sent_run_id: str | None = None
    terminal_error: str | None = None
    started: bool = False
    steps: dict[int, _ChatStep] = field(default_factory=dict)
    completed_steps: dict[int, dict[str, Any]] = field(default_factory=dict)
    tool_calls_by_part: dict[tuple[int, int], _ChatToolCall] = field(default_factory=dict)
    commands: dict[int, _ChatCommandBlock] = field(default_factory=dict)
    timeline: list[tuple[Literal["step", "command"], int]] = field(default_factory=list)
    mutable_block: _ChatMutableBlock | None = None
    child_runs: dict[str, _ChatRun] = field(default_factory=dict)

    def start_step(self, payload: dict[str, Any]) -> None:
        step = _ChatStep.create(payload, run=self)
        index = step.index
        self.remember_timeline("step", index)
        self.steps[index] = step
        self.mutable_block = step

    def update_step(self, payload: Mapping[str, Any]) -> None:
        step = self.steps.get(_chat_step_index(payload))
        if step is not None:
            step.update(payload)

    def delta_step(self, payload: Mapping[str, Any]) -> None:
        step = self.steps.get(_chat_step_index(payload))
        if step is not None:
            step.delta(payload)

    def complete_step(self, payload: dict[str, Any]) -> None:
        index = _chat_step_index(payload)
        self.remember_timeline("step", index)
        active_step = self.steps.get(index)
        completed_payload = active_step.finalize(payload) if active_step is not None else dict(payload)
        self.completed_steps[index] = completed_payload
        self.steps.pop(index, None)
        if self.mutable_block is active_step:
            self.mutable_block = None

    def record_part(self, payload: dict[str, Any]) -> None:
        part = _mapping(payload.get("part"))
        if part.get("type") != "tool_call":
            return
        tool_name = _text(part.get("tool_name")) or _text(part.get("tool_family"))
        if tool_name is None:
            return
        step_index = _chat_step_index(payload)
        part_index = _chat_part_index(payload)
        tool_input = part.get("input")
        self.tool_calls_by_part[(step_index, part_index)] = _ChatToolCall(
            name=tool_name,
            input=dict(tool_input) if isinstance(tool_input, Mapping) else {},
        )

    def record_command(self, payload: Mapping[str, Any]) -> None:
        block = _ChatCommandBlock.create(payload)
        index = block.index
        self.remember_timeline("command", index)
        block.finalize(payload)
        self.commands[index] = block
        if self.mutable_block is block:
            self.mutable_block = None

    def start_command(self, payload: Mapping[str, Any]) -> None:
        command_payload = dict(payload)
        command_payload.setdefault("kind", "start")
        command_payload.setdefault("ref", {"kind": "command", "index": 0})
        if "message" not in command_payload and "input" in command_payload:
            command_payload["message"] = command_payload["input"]
        block = _ChatCommandBlock.create(command_payload)
        self.remember_timeline("command", block.index)
        self.commands[block.index] = block
        self.mutable_block = block

    def finalize_command(self, index: int, payload: Mapping[str, Any]) -> None:
        block = self.commands.get(index)
        if block is None:
            return
        block.finalize(payload)
        if self.mutable_block is block:
            self.mutable_block = None

    def remember_timeline(self, kind: Literal["step", "command"], index: int) -> None:
        item = (kind, index)
        if item not in self.timeline:
            self.timeline.append(item)

    def update_queue(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if run_id := _text(payload.get("run_id")):
            self.run_id = run_id
        if self.cancel_requested:
            self.status = "canceling"
            self.queue_state = None
            self.waiting_reason = None
            self.queue_position = None
            return
        self.status = "waiting"
        self.queue_state = self.status
        self.waiting_reason = _text(payload.get("reason"))
        self.queue_position = _int_or_none(payload.get("position"))

    def mark_running(self) -> None:
        self.started = True
        self.status = "canceling" if self.cancel_requested else "running"
        self.queue_state = None
        self.waiting_reason = None
        self.queue_position = None

    def request_cancel(self) -> None:
        self.cancel_requested = True
        self.status = "canceling"
        self.queue_state = None
        self.waiting_reason = None
        self.queue_position = None

    def clear_cancel_request(self) -> None:
        self.cancel_requested = False
        if self.status == "canceling":
            self.status = "running" if self.started else "submitting"

    def start_child_run(self, payload: Mapping[str, Any]) -> None:
        run_id = _text(payload.get("run_id"))
        if run_id is None:
            return
        child = self.child_runs.get(run_id)
        if child is None:
            child = _ChatRun(
                run_id=run_id,
                message=_event_message_text(payload.get("input")),
                status="running",
                executable_kind=_text(payload.get("executable_kind")) or "thunk",
                executable_name=_text(payload.get("executable_name")),
                accept_child_trace=True,
            )
            self.child_runs[run_id] = child
        else:
            child.status = "running"
            child.executable_kind = _text(payload.get("executable_kind")) or child.executable_kind
            child.executable_name = _text(payload.get("executable_name")) or child.executable_name

    def child_run(self, run_id: str | None) -> _ChatRun | None:
        if run_id is None:
            return None
        return self.child_runs.get(run_id)

    def tick(self) -> None:
        for step in self.steps.values():
            step.frame += 1

    def step_indexes(self) -> list[int]:
        return sorted(set(self.steps) | set(self.completed_steps))


class _ChatLastRunPanel:
    def __init__(self, get_run: Callable[[], _ChatRun | None]) -> None:
        self.get_run = get_run
        self.user_view = FormattedTextControl(self.render_user)
        self.activity_view = FormattedTextControl(self.render_activity)

    def container(self) -> ConditionalContainer:
        return ConditionalContainer(
            HSplit(
                [
                    Window(
                        self.user_view,
                        height=self.user_rows,
                        wrap_lines=False,
                        always_hide_cursor=True,
                        style="class:normal-input",
                        char=" ",
                    ),
                    Window(
                        self.activity_view,
                        height=self.activity_rows,
                        wrap_lines=False,
                        always_hide_cursor=True,
                    ),
                ],
                height=self.height_dimension,
                window_too_small=Window(always_hide_cursor=True),
            ),
            filter=Condition(lambda: bool(self.lines())),
        )

    def render_user(self) -> list[tuple[str, str]]:
        run = self.get_run()
        if run is None:
            return []
        return _chat_input_bar_fragments(_chat_run_input_bar_spec(run))

    def render_activity(self) -> list[tuple[str, str]]:
        run = self.get_run()
        if run is None:
            return []
        rows = _chat_active_activity_fragment_rows(run, self.step_line)
        rows = [[], *rows, []]
        return _chat_join_fragment_rows(rows)

    def lines(self) -> list[str]:
        return [*self.user_lines(), *self.activity_lines()]

    def user_lines(self) -> list[str]:
        run = self.get_run()
        if run is None:
            return []
        return _chat_panel_user_block(run)

    def activity_lines(self) -> list[str]:
        run = self.get_run()
        if run is None:
            return []
        lines = _chat_run_activity_lines(run, self.step_line)
        return ["", *lines, ""]

    def step_line(self, run: _ChatRun, index: int) -> str:
        if index in run.completed_steps:
            return _chat_completed_step_line(run.completed_steps[index], run=run)
        return _chat_active_step_line(run.steps[index])

    def rows(self) -> int:
        return len(self.lines())

    def height_dimension(self) -> Dimension:
        return Dimension(min=0, preferred=self.rows(), weight=1)

    def user_rows(self) -> int:
        return len(self.user_lines())

    def activity_rows(self) -> int:
        return len(self.activity_lines())


class _ChatSubmissionQueue:
    def __init__(self, get_items: Callable[[], list[_ChatQueueItem]]) -> None:
        self.get_items = get_items
        self.view = FormattedTextControl(self.render)

    def container(self) -> ConditionalContainer:
        return ConditionalContainer(
            Window(
                self.view,
                height=self.rows,
                wrap_lines=False,
                always_hide_cursor=True,
                style="class:queue",
                char=" ",
            ),
            filter=Condition(lambda: bool(self.get_items())),
        )

    def render(self) -> list[tuple[str, str]]:
        return _chat_queue_fragments(self.get_items())

    def lines(self) -> list[str]:
        items = self.get_items()
        indexed = list(enumerate(items, 1))
        shown = indexed[:_CHAT_MAX_QUEUE_ROWS]
        hidden = len(items) - len(shown)
        summary = "  queued for submission:"
        if hidden:
            summary += f" ({hidden} more not shown)"
        return [summary, *[f"  [{index}] {_chat_summarize(item.text)}" for index, item in shown]]

    def rows(self) -> int:
        return len(self.lines()) if self.get_items() else 0

    def height_dimension(self) -> Dimension:
        return _chat_fixed_height(self.rows(), minimum=0)


def _chat_queue_fragments(items: Sequence[_ChatQueueItem]) -> list[tuple[str, str]]:
    shown = list(enumerate(items, 1))[:_CHAT_MAX_QUEUE_ROWS]
    hidden = len(items) - len(shown)
    title = "  queued for submission:"
    if hidden:
        title += f" ({hidden} more not shown)"
    rows: list[list[tuple[str, str]]] = [[("class:queue.dim", title)]]
    rows.extend(
        [
            ("class:queue", "  "),
            ("class:queue.dim", f"[{index}]"),
            ("class:queue", f" {_chat_summarize(item.text)}"),
        ]
        for index, item in shown
    )

    fragments: list[tuple[str, str]] = []
    for row_index, row in enumerate(rows):
        visible_len = 0
        for style, text in row:
            fragments.append((style, text))
            visible_len += _chat_display_len(text)
        padding = " " * max(0, _chat_terminal_width() - visible_len)
        if padding:
            fragments.append(("class:queue", padding))
        if row_index < len(rows) - 1:
            fragments.append(("", "\n"))
    return fragments


class _ChatPromptBox:
    def __init__(
        self,
        emit: Callable[[_ChatUIEvent], None],
        invalidate: Callable[[], None],
        status_label: str,
        *,
        history_store: ChatInputHistoryStore | None = None,
    ) -> None:
        self.emit = emit
        self.invalidate = invalidate
        self.status_label = status_label
        self.history = InMemoryHistory()
        self.history_store = history_store
        for entry in history_store.load() if history_store is not None else ():
            self.history.append_string(entry)
        self.buffer = Buffer(multiline=True, history=self.history)
        self.error_message = ""
        self.history_index: int | None = None
        self.history_draft = ""
        self.status = FormattedTextControl(self.render_status)
        self.buffer.on_text_changed += self.handle_text_changed

    def container(self) -> HSplit:
        return HSplit(
            [
                Window(height=1, style="class:input", always_hide_cursor=True, char=" "),
                VSplit(
                    [
                        Window(FormattedTextControl(ANSI(f"{_CHAT_DIM}> {_CHAT_NORMAL_INTENSITY}")), width=2, style="class:input", char=" "),
                        Window(
                            BufferControl(buffer=self.buffer),
                            height=self.input_rows,
                            wrap_lines=True,
                            style="class:input",
                            char=" ",
                        ),
                    ],
                    height=self.input_rows,
                    style="class:input",
                ),
                Window(height=1, style="class:input", always_hide_cursor=True, char=" "),
                Window(self.status, height=1, style="class:status", always_hide_cursor=True, char=" "),
            ],
            height=self.height_dimension,
            window_too_small=self.compact_container(),
        )

    def compact_container(self) -> VSplit:
        return VSplit(
            [
                Window(FormattedTextControl(ANSI(f"{_CHAT_DIM}> {_CHAT_NORMAL_INTENSITY}")), width=2, style="class:input", char=" "),
                Window(
                    BufferControl(buffer=self.buffer),
                    height=1,
                    wrap_lines=False,
                    style="class:input",
                    char=" ",
                ),
            ],
            height=1,
            style="class:input",
        )

    def render_status(self) -> list[tuple[str, str]]:
        if self.error_message:
            return [("class:status.error", f"  ! {self.error_message}  ")]
        segments = _chat_status_segments(self.status_label)
        if segments:
            style, text = segments[0]
            segments[0] = (style, f"  {text}")
        return [
            *segments,
            ("class:status.text", "  ^c cancel  ^d exit  ^j newline  ↑↓ history"),
            ("class:status.text", "  "),
        ]

    def bind(self, keys: KeyBindings) -> None:
        @keys.add("enter")
        def submit(_event: Any) -> None:
            message = self.buffer.text.strip()
            if not message:
                return
            self.record_history(message)
            self.buffer.text = ""
            self.history_index = None
            self.history_draft = ""
            self.emit(_ChatUIEvent("submit", message))
            self.invalidate()

        @keys.add("c-c")
        def interrupt(_event: Any) -> None:
            self.emit(_ChatUIEvent("interrupt"))

        @keys.add("c-d")
        def eof(_event: Any) -> None:
            self.emit(_ChatUIEvent("eof"))

        @keys.add("c-q")
        def quit_app(_event: Any) -> None:
            self.emit(_ChatUIEvent("quit"))

        @keys.add("c-l")
        def clear_screen(_event: Any) -> None:
            self.emit(_ChatUIEvent("clear"))

        @keys.add("c-j")
        @keys.add("escape", "enter")
        def insert_newline(_event: Any) -> None:
            self.insert_newline()

        @keys.add("escape", "escape")
        def cancel_run(_event: Any) -> None:
            self.emit(_ChatUIEvent("cancel"))

        @keys.add("up")
        @keys.add("c-p")
        def previous_history(_event: Any) -> None:
            self.previous_history()

        @keys.add("down")
        @keys.add("c-n")
        def next_history(_event: Any) -> None:
            self.next_history()

        try:
            keys.add("s-enter")(lambda _event: self.insert_newline())
        except ValueError:
            pass

    def insert_newline(self) -> None:
        self.buffer.insert_text("\n")
        self.invalidate()

    def has_input(self) -> bool:
        return bool(self.buffer.text)

    def clear_input(self) -> None:
        if not self.buffer.text:
            return
        self.buffer.text = ""
        self.history_index = None
        self.history_draft = ""
        self.invalidate()

    def record_history(self, message: str) -> None:
        entries = self.history_entries()
        if not entries or entries[-1] != message:
            self.history.append_string(message)
            if self.history_store is not None:
                try:
                    self.history_store.append(message)
                except OSError:
                    pass

    def previous_history(self) -> None:
        if self.buffer.document.cursor_position_row > 0:
            self.buffer.cursor_up()
            return
        entries = self.history_entries()
        if not entries:
            return
        if self.history_index is None:
            self.history_draft = self.buffer.text
            self.history_index = len(entries) - 1
        else:
            self.history_index = max(0, self.history_index - 1)
        self.replace_input(entries[self.history_index])

    def next_history(self) -> None:
        if self.buffer.document.cursor_position_row < self.buffer.document.line_count - 1:
            self.buffer.cursor_down()
            return
        if self.history_index is None:
            return
        entries = self.history_entries()
        if self.history_index < len(entries) - 1:
            self.history_index += 1
            self.replace_input(entries[self.history_index])
        else:
            self.history_index = None
            self.replace_input(self.history_draft)
            self.history_draft = ""

    def history_entries(self) -> list[str]:
        return list(self.history.get_strings())

    def replace_input(self, text: str) -> None:
        self.buffer.text = text
        self.buffer.cursor_position = len(text)
        self.invalidate()

    def handle_text_changed(self, _buffer: Buffer) -> None:
        if self.error_message:
            self.error_message = ""
        if self.history_index is not None:
            entries = self.history_entries()
            if self.buffer.text != entries[self.history_index]:
                self.history_index = None
                self.history_draft = ""

    def set_error(self, message: str) -> None:
        self.error_message = message
        self.invalidate()

    def clear_error(self) -> None:
        if self.error_message:
            self.error_message = ""
            self.invalidate()

    def input_rows(self) -> int:
        return min(_CHAT_MAX_INPUT_ROWS, max(1, self.buffer.document.line_count))

    def rows(self) -> int:
        return self.input_rows() + 3

    def height_dimension(self) -> Dimension:
        return Dimension(min=1, preferred=self.rows(), weight=8)


class _ChatBottomApp:
    """Use terminal scrollback for history and prompt-toolkit for the bottom UI."""

    def __init__(
        self,
        ctx: typer.Context,
        *,
        thread_id: str | None,
        selector_payload: dict[str, object],
        deps: ChatTuiDependencies,
    ) -> None:
        self.ctx = ctx
        self.deps = deps
        self.thread_id = thread_id
        self.selector_payload = selector_payload
        self.events: asyncio.Queue[_ChatUIEvent] = asyncio.Queue()
        self.pending: list[_ChatQueueItem] = []
        self.active_run: _ChatRun | None = None
        self.local_streaming = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.dispatcher: asyncio.Task[None] | None = None
        self.ticker: asyncio.Task[None] | None = None
        self.stream_step_index: int | None = None
        self.stream_text_parts: list[str] = []
        self.stream_tool_steps: dict[str, int] = {}
        self.model_label = _chat_resolved_model_label(ctx, self.selector_payload, deps=self.deps)
        self.home_label = self.deps.home_label(ctx)

        self.last_run_panel = _ChatLastRunPanel(lambda: self.active_run)
        self.queue_panel = _ChatSubmissionQueue(lambda: self.pending)
        self.prompt = _ChatPromptBox(
            self.emit,
            self.invalidate,
            self.status_label(),
            history_store=self.deps.input_history_store(ctx),
        )
        self.app = Application(
            layout=self.build_layout(),
            key_bindings=self.build_keys(),
            style=self.build_style(),
            full_screen=False,
            erase_when_done=True,
            mouse_support=False,
        )

    def status_label(self) -> str:
        model_label = self.model_label
        executable_label = _chat_executable_status_label(self.selector_payload)
        if executable_label:
            return f"{model_label}  {executable_label}"
        return model_label

    def build_layout(self) -> Layout:
        root = HSplit(
            [
                self.last_run_panel.container(),
                self.queue_panel.container(),
                self.prompt.container(),
            ],
            height=self.bottom_dimension,
            window_too_small=self.prompt.compact_container(),
        )
        return Layout(root, focused_element=self.prompt.buffer)

    def build_keys(self) -> KeyBindings:
        keys = KeyBindings()
        self.prompt.bind(keys)
        return keys

    def emit(self, event: _ChatUIEvent) -> None:
        self.events.put_nowait(event)

    def emit_from_thread(self, event: _ChatUIEvent) -> None:
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self.events.put_nowait, event)

    def invalidate(self) -> None:
        if hasattr(self, "app"):
            self.app.invalidate()

    def build_style(self) -> Style:
        return Style.from_dict(_chat_ui_palette())

    def bottom_rows(self) -> int:
        return self.last_run_panel.rows() + self.queue_panel.rows() + self.prompt.rows()

    def bottom_dimension(self) -> Dimension:
        return Dimension(min=1, preferred=self.bottom_rows(), weight=1)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.print_header()
        self.dispatcher = asyncio.create_task(self.dispatch_events())
        self.ticker = asyncio.create_task(self.emit_ticks())
        try:
            with patch_stdout(raw=True):
                await self.app.run_async()
        finally:
            self.events.put_nowait(_ChatUIEvent("quit"))
            await self.stop_tasks()

    async def stop_tasks(self) -> None:
        if self.ticker and not self.ticker.done():
            self.ticker.cancel()
        if self.dispatcher and not self.dispatcher.done():
            await self.dispatcher

    async def emit_ticks(self) -> None:
        while True:
            await asyncio.sleep(0.12)
            if self.active_run and self.active_run.steps:
                self.events.put_nowait(_ChatUIEvent("tick"))

    async def dispatch_events(self) -> None:
        while True:
            event = await self.events.get()
            try:
                if event.type == "submit":
                    self.handle_submit(str(event.value))
                elif event.type == "runtime" and isinstance(event.value, dict):
                    self.handle_runtime_event(event.value)
                elif event.type == "error":
                    self.handle_error(str(event.value or "runtime request failed"))
                elif event.type == "cancel_error":
                    self.handle_cancel_error(str(event.value or "cancel request failed"))
                elif event.type == "tick":
                    self.handle_tick()
                elif event.type == "interrupt":
                    self.handle_interrupt()
                elif event.type == "eof":
                    self.handle_eof()
                elif event.type == "cancel":
                    self.handle_cancel()
                elif event.type == "clear":
                    self.handle_clear()
                elif event.type == "quit":
                    self.handle_quit()
                    return
            finally:
                self.events.task_done()

    def handle_submit(self, message: str) -> None:
        self.prompt.clear_error()
        if self.handle_local_command(message):
            self.app.invalidate()
            return
        if self.has_active_run():
            self.pending.append(_ChatQueueItem(kind="run", text=message))
        else:
            self.start_run(message)
        self.app.invalidate()

    def handle_clear(self) -> None:
        if self.has_active_run():
            self.prompt.set_error("Cannot clear while a run is active.")
            return
        self.prompt.clear_error()
        self.app.renderer.clear()
        self.app.invalidate()

    def handle_interrupt(self) -> None:
        if self.prompt.has_input():
            self.prompt.clear_input()
            return
        if self.has_active_run():
            self.handle_cancel()
            return
        self.prompt.clear_error()
        self.app.invalidate()

    def handle_eof(self) -> None:
        if not self.prompt.has_input() and not self.has_active_run():
            self.handle_quit()
            return
        self.app.invalidate()

    def handle_cancel(self) -> None:
        if not self.has_active_run():
            return
        if self.active_run is None or self.active_run.cancel_requested:
            return
        self.prompt.clear_error()
        self.active_run.request_cancel()
        self.maybe_send_cancel_request()
        self.app.invalidate()

    def handle_quit(self) -> None:
        if self.app.is_running:
            self.app.exit()

    def handle_tick(self) -> None:
        if self.active_run and self.active_run.steps:
            self.active_run.tick()
            self.app.invalidate()

    def handle_error(self, message: str) -> None:
        friendly = _chat_friendly_error(message)
        if self.active_run:
            self.active_run.status = "error"
            self.active_run.terminal_error = friendly
            _chat_record_system_event(self.active_run, f"error: {friendly}", clear_active=True)
            self.print_run(self.active_run)
        else:
            self.prompt.set_error(friendly)
        self.active_run = None
        self.start_next_run()
        self.app.invalidate()

    def handle_cancel_error(self, message: str) -> None:
        friendly = _chat_friendly_error(message)
        if self.active_run is None:
            self.prompt.set_error(friendly)
            self.app.invalidate()
            return
        self.active_run.clear_cancel_request()
        _chat_record_system_event(self.active_run, f"error: cancel failed: {friendly}", clear_active=False)
        self.app.invalidate()

    def maybe_send_cancel_request(self) -> None:
        run = self.active_run
        if run is None or not run.cancel_requested or not run.started or not run.run_id:
            return
        if run.cancel_sent_run_id == run.run_id:
            return
        run.cancel_sent_run_id = run.run_id
        self.send_cancel_request(run.run_id)

    def send_cancel_request(self, run_id: str) -> None:
        def consume() -> None:
            try:
                self.deps.runtime_post(
                    self.ctx,
                    f"/api/v1/runs/{run_id}/cancel",
                    payload={},
                )
            except click.ClickException as exc:
                self.emit_from_thread(_ChatUIEvent("cancel_error", exc.message))
            except Exception as exc:  # pragma: no cover - defensive cross-thread reporting
                self.emit_from_thread(_ChatUIEvent("cancel_error", f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=consume, daemon=True).start()

    def handle_runtime_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or event.get("event_type") or "")
        if self.handle_chat_stream_event(event_type, event):
            self.app.invalidate()
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        if self.handle_child_trace_event(event_type, payload):
            self.app.invalidate()
            return
        if self.should_ignore_trace_event(event_type, payload):
            return
        if event_type == "run_waiting":
            self.handle_queue_event(event_type, payload)
        elif event_type == "run_starting":
            self.handle_run_starting(payload)
        elif event_type == "run_steering":
            self.handle_run_steering(payload)
        elif event_type == "run_stopping":
            self.handle_run_stopping(payload)
        elif event_type == "run_begin":
            self.handle_run_begin(payload)
        elif event_type == "step_begin" and self.active_run:
            self.active_run.start_step(payload)
        elif event_type == "part_begin" and self.active_run:
            self.active_run.update_step(payload)
        elif event_type == "part_delta" and self.active_run:
            self.active_run.delta_step(payload)
        elif event_type == "part_end" and self.active_run:
            self.active_run.record_part(payload)
        elif event_type == "step_end" and self.active_run:
            self.active_run.complete_step(payload)
        elif event_type == "run_end":
            self.finish_run(payload)
        self.app.invalidate()

    def should_ignore_trace_event(self, event_type: str, payload: Mapping[str, Any]) -> bool:
        run_id = _text(payload.get("run_id"))
        if event_type in {"run_starting", "run_waiting"}:
            return self.active_run is not None and bool(self.active_run.run_id) and run_id is not None and run_id != self.active_run.run_id
        if event_type in {"run_steering", "run_stopping"}:
            return self.active_run is not None and bool(self.active_run.run_id) and run_id != self.active_run.run_id
        if event_type == "run_begin":
            parent_run_id = _text(payload.get("parent_run_id"))
            call_kind = _text(payload.get("call_kind")) or "top"
            if parent_run_id or call_kind != "top":
                return True
        if event_type in {"run_begin", "step_begin", "part_begin", "part_delta", "part_end", "step_end", "run_end"}:
            return self.active_run is not None and bool(self.active_run.run_id) and run_id != self.active_run.run_id
        return False

    def handle_queue_event(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self.active_run is None:
            self.active_run = _ChatRun(
                run_id=_text(payload.get("run_id")) or "",
                message="",
                status="queued",
                accept_child_trace=True,
            )
        self.active_run.update_queue(event_type, payload)

    def handle_child_trace_event(self, event_type: str, payload: Mapping[str, Any]) -> bool:
        if self.active_run is None or not self.active_run.accept_child_trace:
            return False
        run_id = _text(payload.get("run_id"))
        if event_type == "run_begin":
            parent_run_id = _text(payload.get("parent_run_id"))
            root_run_id = _text(payload.get("root_run_id"))
            if parent_run_id == self.active_run.run_id or root_run_id == self.active_run.run_id or parent_run_id in self.active_run.child_runs:
                self.active_run.start_child_run(payload)
                return True
            return False
        child = self.active_run.child_run(run_id)
        if child is None:
            return False
        if event_type == "step_begin":
            child.start_step(dict(payload))
            return True
        if event_type == "part_end":
            child.record_part(dict(payload))
            return True
        if event_type == "part_begin":
            child.update_step(payload)
            return True
        if event_type == "part_delta":
            child.delta_step(payload)
            return True
        if event_type == "step_end":
            child.complete_step(dict(payload))
            return True
        if event_type == "run_end":
            child.status = _display_run_status(payload.get("status")) or "completed"
            error = _text(payload.get("error"))
            if error:
                child.terminal_error = _chat_friendly_error(error)
            if child.status in {"failed", "error", "canceled", "cancelled"}:
                message = _chat_stopped_run_message(child.status, child.terminal_error if error else None)
                if error:
                    message = f"error: {message}"
                _chat_record_system_event(child, message, clear_active=True)
            return True
        return False

    def handle_chat_stream_event(self, event_type: str, event: Mapping[str, Any]) -> bool:
        if event_type == "start":
            self.handle_chat_stream_start(event)
            return True
        if event_type == "message-metadata":
            self.handle_chat_stream_metadata(event)
            return True
        if event_type in {"start-step", "text-start"}:
            self.start_chat_stream_step()
            return True
        if event_type == "text-delta":
            self.append_chat_stream_text(event)
            return True
        if event_type == "finish-step":
            self.complete_chat_stream_step()
            return True
        if event_type == "tool-input-available":
            self.record_chat_stream_tool_request(event)
            return True
        if event_type == "tool-output-available":
            self.record_chat_stream_tool_result(event)
            return True
        if event_type == "error":
            self.handle_error(_text(event.get("errorText")) or _text(event.get("error")) or "runtime request failed")
            return True
        if event_type == "finish":
            if self.active_run is None:
                return True
            self.complete_chat_stream_step()
            status = "canceled" if self.active_run.cancel_requested else "finished"
            self.finish_run(
                {
                    "run_id": self.active_run.run_id if self.active_run is not None else "",
                    "status": status,
                }
            )
            return True
        return event_type == "text-end"

    def handle_chat_stream_start(self, event: Mapping[str, Any]) -> None:
        metadata = _mapping(event.get("messageMetadata"))
        run_id = _text(metadata.get("run_id")) or _text(event.get("messageId")) or ""
        thread_id = _text(metadata.get("thread_id"))
        if thread_id:
            self.thread_id = thread_id
        if self.active_run is None:
            self.active_run = _ChatRun(run_id=run_id, message="", status="running", accept_child_trace=True)
            self.active_run.mark_running()
            self.maybe_send_cancel_request()
            return
        if run_id:
            self.active_run.run_id = run_id
        self.active_run.mark_running()
        self.maybe_send_cancel_request()

    def handle_chat_stream_metadata(self, event: Mapping[str, Any]) -> None:
        metadata = _mapping(event.get("messageMetadata"))
        thread_id = _text(metadata.get("thread_id"))
        if thread_id:
            self.thread_id = thread_id
        run_id = _text(metadata.get("run_id"))
        if run_id and self.active_run is not None:
            self.active_run.run_id = run_id
            self.maybe_send_cancel_request()

    def start_chat_stream_step(self) -> None:
        if self.active_run is None:
            return
        if self.stream_step_index is not None:
            return
        index = self.next_chat_stream_step_index()
        self.stream_step_index = index
        self.stream_text_parts = []
        self.active_run.start_step({"step_index": index, "kind": "model"})

    def append_chat_stream_text(self, event: Mapping[str, Any]) -> None:
        if self.active_run is None:
            return
        self.start_chat_stream_step()
        delta = _text(event.get("delta"))
        if delta:
            self.stream_text_parts.append(delta)

    def complete_chat_stream_step(self) -> None:
        if self.active_run is None or self.stream_step_index is None:
            return
        index = self.stream_step_index
        text = "".join(self.stream_text_parts)
        output: list[dict[str, object]] = []
        if text:
            output.append({"type": "text", "text": text})
        self.active_run.complete_step(
            {
                "run_id": self.active_run.run_id,
                "step_index": index,
                "kind": "model",
                "output": output,
            }
        )
        self.stream_step_index = None
        self.stream_text_parts = []

    def record_chat_stream_tool_request(self, event: Mapping[str, Any]) -> None:
        if self.active_run is None:
            return
        self.complete_chat_stream_step()
        index = self.next_chat_stream_step_index()
        tool_call_id = _text(event.get("toolCallId")) or f"tool_{index}"
        tool_name = _text(event.get("toolName")) or "tool"
        tool_input = event.get("input")
        part = {
            "type": "tool_call",
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "input": dict(tool_input) if isinstance(tool_input, Mapping) else {},
        }
        self.active_run.record_part({"step_index": index, "part_index": 0, "part": part})
        self.stream_tool_steps[tool_call_id] = index
        self.active_run.start_step(
            {
                "run_id": self.active_run.run_id,
                "step_index": index,
                "kind": "tool",
                "input": [part],
            }
        )

    def record_chat_stream_tool_result(self, event: Mapping[str, Any]) -> None:
        if self.active_run is None:
            return
        self.complete_chat_stream_step()
        tool_call_id = _text(event.get("toolCallId"))
        index = self.stream_tool_steps.pop(tool_call_id, None) if tool_call_id is not None else None
        if index is None:
            index = self.next_chat_stream_step_index()
        tool_name = _text(event.get("toolName")) or "tool"
        input_part = None
        active_step = self.active_run.steps.get(index)
        if active_step is not None:
            for item in _list(active_step.payload.get("input")):
                if isinstance(item, Mapping):
                    input_part = dict(item)
                    break
        if input_part is None:
            input_part = {"tool_name": tool_name, "input": {}}
        self.active_run.complete_step(
            {
                "run_id": self.active_run.run_id,
                "step_index": index,
                "kind": "tool",
                "input": [input_part],
                "output": [
                    {
                        "type": "tool_result",
                        "tool_call_id": tool_call_id or "",
                        "tool_name": tool_name,
                        "output": event.get("output"),
                    }
                ],
            }
        )

    def next_chat_stream_step_index(self) -> int:
        if self.active_run is None:
            return 1
        return max(self.active_run.step_indexes(), default=0) + 1

    def handle_run_starting(self, payload: dict[str, Any]) -> None:
        run_id = str(payload.get("run_id") or "")
        message = _event_message_text(payload.get("input"))
        if not message:
            message = self.active_run.message if self.active_run is not None else ""
        if self.active_run and (not self.active_run.run_id or self.active_run.run_id == run_id):
            self.active_run.run_id = run_id
            self.active_run.message = message
            self.active_run.start_command(payload)
            return
        self.active_run = _ChatRun(run_id=run_id, message=message, status="submitting", accept_child_trace=True)
        self.active_run.start_command(payload)

    def handle_run_steering(self, payload: dict[str, Any]) -> None:
        if self.active_run is not None:
            command = dict(payload)
            command["kind"] = "steer"
            self.active_run.record_command(command)

    def handle_run_stopping(self, payload: dict[str, Any]) -> None:
        if self.active_run is not None:
            command = dict(payload)
            command["kind"] = "stop"
            self.active_run.record_command(command)
            self.active_run.request_cancel()

    def handle_run_begin(self, payload: dict[str, Any]) -> None:
        run_id = str(payload.get("run_id") or "")
        if self.active_run and (not self.active_run.run_id or self.active_run.run_id == run_id):
            self.active_run.run_id = run_id
            self.active_run.finalize_command(0, payload)
            self.active_run.mark_running()
            self.maybe_send_cancel_request()
            return
        message = _event_message_text(payload.get("input"))
        self.active_run = _ChatRun(run_id=run_id, message=message, status="running")
        self.active_run.finalize_command(0, payload)
        self.active_run.mark_running()
        self.maybe_send_cancel_request()

    def finish_run(self, payload: dict[str, Any]) -> None:
        completed_run = self.active_run
        if completed_run is not None:
            completed_run.status = _display_run_status(payload.get("status")) or "completed"
            error = _text(payload.get("error"))
            if error:
                completed_run.terminal_error = _chat_friendly_error(error)
            if completed_run.status in {"failed", "error", "canceled", "cancelled"}:
                message = _chat_stopped_run_message(
                    completed_run.status,
                    completed_run.terminal_error if error else None,
                )
                if error:
                    message = f"error: {message}"
                _chat_record_system_event(completed_run, message, clear_active=True)
        self.active_run = None
        self.local_streaming.clear()
        self.prompt.clear_error()
        if completed_run is not None:
            self.print_run(completed_run)
        self.start_next_run()
        self.app.invalidate()

    def start_next_run(self) -> None:
        if self.pending:
            item = self.pending.pop(0)
            self.start_run(item.text)

    def handle_local_command(self, message: str) -> bool:
        parsed = _chat_local_command(message)
        if parsed is None:
            return False
        command, argument = parsed
        if command in {"exit", "quit"}:
            self.handle_quit()
            return True
        if command in {"help", "?"}:
            self.deps.write_lines(_chat_local_command_lines(message, _chat_help_lines()))
            return True
        if command in {"queue", "q"}:
            return self.handle_queue_command(argument, message)
        if command in {"thunk", "flow"}:
            return self.handle_executable_command(command, argument, message)
        if command not in {"model", "models"}:
            self.prompt.set_error(f"Unknown command: /{command}")
            return True
        if argument:
            if self.has_active_run():
                self.prompt.set_error("Cannot change model while a run is active.")
                return True
            selectors = _chat_model_command_selectors(argument)
            if not selectors:
                self.prompt.set_error("/model requires a selector.")
                return True
            labels = _chat_resolve_model_command_labels(self.ctx, selectors, deps=self.deps)
            if labels is None:
                self.prompt.set_error(f"Model selector matched no models: {', '.join(selectors)}")
                return True
            self.selector_payload["models"] = list(selectors)
            self.model_label = ", ".join(labels)
            self.prompt.status_label = self.status_label()
            self.deps.write_lines(_chat_local_command_lines(message, [f"model: {self.model_label}"]))
            return True
        try:
            payload = self.deps.runtime_json(self.ctx, "/api/v1/chat/models")
        except click.ClickException as exc:
            self.prompt.set_error(_chat_friendly_error(exc.message))
            return True
        self.deps.write_lines(_chat_local_command_lines(message, ["available models", *_chat_model_list_lines(payload)]))
        return True

    def handle_queue_command(self, argument: str, message: str) -> bool:
        tokens = argument.split()
        if not tokens:
            self.deps.write_lines(_chat_local_command_lines(message, _chat_queue_help_lines()))
            return True
        action = tokens[0].lower()
        if action in {"clear", "c"}:
            self.pending.clear()
            return True
        if action not in {"steer", "s", "delete", "d", "edit", "e"}:
            self.prompt.set_error(f"Unknown queue command: {tokens[0]}")
            return True
        if len(tokens) < 2:
            self.prompt.set_error(f"/queue {tokens[0]} requires an item number.")
            return True
        index = _chat_queue_command_index(tokens[1], len(self.pending))
        if index is None:
            self.prompt.set_error(f"Queue item not found: {tokens[1]}")
            return True
        item = self.pending[index]
        if action in {"delete", "d"}:
            self.pending.pop(index)
            return True
        if action in {"edit", "e"}:
            self.pending.pop(index)
            self.prompt.replace_input(item.text)
            return True
        run = self.active_run
        if run is None or not run.run_id:
            self.prompt.set_error("No active run to steer.")
            return True
        try:
            self.deps.runtime_post(
                self.ctx,
                f"/api/v1/runs/{run.run_id}/steer",
                payload={"message": self.deps.message_payload(item.text)},
            )
        except click.ClickException as exc:
            self.prompt.set_error(_chat_friendly_error(exc.message))
            return True
        self.pending.pop(index)
        return True

    def handle_executable_command(self, command: str, argument: str, message: str) -> bool:
        if argument:
            if self.has_active_run():
                self.prompt.set_error(f"Cannot change {command} while a run is active.")
                return True
            _chat_set_executable_selector(self.selector_payload, kind=command, name=argument)
            self.prompt.status_label = self.status_label()
            self.deps.write_lines(_chat_local_command_lines(message, [f"{command}: {argument}"]))
            return True
        try:
            payload = self.deps.runtime_json(self.ctx, f"/api/v1/chat/{command}s")
        except click.ClickException as exc:
            self.prompt.set_error(_chat_friendly_error(exc.message))
            return True
        selected = _text(self.selector_payload.get(command))
        self.deps.write_lines(
            _chat_local_command_lines(
                message,
                [f"available {command}s", *_chat_executable_list_lines(payload, selected=selected)],
            )
        )
        return True

    def start_run(self, message: str) -> None:
        self.active_run = _ChatRun(run_id="", message=message, status="submitting", accept_child_trace=True)
        try:
            thread_id = self.ensure_thread_id()
        except click.ClickException as exc:
            self.handle_error(exc.message)
            return
        request_id = f"term_{uuid4().hex}"
        payload: dict[str, Any] = {
            "thread": thread_id,
            "client": "tui",
            "request_id": request_id,
            "message": self.deps.message_payload(message),
            **self.selector_payload,
        }

        def consume() -> None:
            self.local_streaming.set()
            try:
                self.deps.runtime_consume_stream(
                    self.ctx,
                    "/api/v1/chat/stream",
                    payload=payload,
                    event_handler=lambda event: self.emit_from_thread(_ChatUIEvent("runtime", event)),
                )
            except click.ClickException as exc:
                self.emit_from_thread(_ChatUIEvent("error", exc.message))
            except Exception as exc:
                self.emit_from_thread(_ChatUIEvent("error", f"{type(exc).__name__}: {exc}"))
            finally:
                self.local_streaming.clear()

        threading.Thread(target=consume, daemon=True).start()

    def ensure_thread_id(self) -> str:
        if self.thread_id is None:
            result = self.deps.runtime_post(self.ctx, "/api/v1/threads", payload={"client": "tui"})
            created = result.get("thread_id")
            if not isinstance(created, str):
                raise click.ClickException("runtime did not return a thread id")
            self.thread_id = created
        return self.thread_id

    def has_active_run(self) -> bool:
        return self.active_run is not None or self.local_streaming.is_set()

    def print_header(self) -> None:
        self.deps.write_lines(_chat_header_lines(self.status_label(), home_label=self.home_label), hide_cursor=False)

    def print_run(self, run: _ChatRun) -> None:
        lines = _chat_run_lines(run, include_steps=True)
        self.deps.write_lines(lines)


def _chat_interactive_prompt_toolkit(
    ctx: typer.Context,
    *,
    thread_id: str | None,
    selector_payload: dict[str, object] | None = None,
    deps: ChatTuiDependencies,
) -> None:
    asyncio.run(_ChatBottomApp(ctx, thread_id=thread_id, selector_payload=dict(selector_payload or {}), deps=deps).run())


def _chat_step_index(payload: Mapping[str, Any]) -> int:
    value = payload.get("step_index")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    return 0


def _chat_block_index(payload: Mapping[str, Any]) -> int:
    if "step_index" in payload:
        return _chat_step_index(payload)
    return _chat_command_index(payload)


def _chat_command_index(payload: Mapping[str, Any]) -> int:
    ref = _mapping(payload.get("ref"))
    for value in (ref.get("index"), payload.get("index")):
        index = _int_or_none(value)
        if index is not None:
            return index
    return 0


def _chat_part_index(payload: Mapping[str, Any]) -> int:
    value = payload.get("part_index")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    return 0


def _chat_step_label(payload: Mapping[str, Any], run: _ChatRun | None = None) -> str:
    kind = str(payload.get("kind") or "")
    step_payload = _mapping(payload.get("payload"))
    if kind == "model":
        return "thinking..."
    if kind == "tool":
        return f"running {_chat_tool_call_display(_chat_tool_call(payload, run=run))}"
    if kind == "run":
        target = executable_label(
            _text(step_payload.get("target_kind")) or "run",
            _text(step_payload.get("target")),
            metadata=_mapping(step_payload.get("metadata")),
        ).replace(":", " ", 1)
        return f"running {target}"
    if kind in {"step", "parallel", "bind"}:
        op = _text(step_payload.get("op")) or "flow"
        return f"running {op}"
    if kind == "system":
        return _text(step_payload.get("message")) or _text(step_payload.get("op")) or kind
    return "running"


def _chat_active_step_line(step: _ChatStep) -> str:
    text_delta = step.text_delta()
    if step.kind == "model" and text_delta:
        preview = _chat_summarize(" ".join(_chat_visible_text(text_delta).split()), width=120)
        return _chat_progress_tail(f"{_chat_marker_for(step.kind)} {preview}")
    line = _chat_progress_tail(f"{_chat_marker_for(step.kind)} {step.label}")
    if step.kind == "model":
        return line
    return _chat_dim(line)


def _chat_completed_step_line(payload: Mapping[str, Any], *, run: _ChatRun | None = None) -> str:
    kind = str(payload.get("kind") or "")
    step_payload = _mapping(payload.get("payload"))
    marker = _chat_marker_for(kind)
    if kind == "model":
        text = _event_parts_text(payload.get("output"))
        if text:
            return f"{marker} assistant message"
        requests = _chat_model_tool_request_summary(payload, run=run)
        if requests:
            return f"{marker} requested {requests}"
        model = _text(step_payload.get("model_ref")) or _text(step_payload.get("model"))
        return f"{marker} [no text message]{f' ({model})' if model else ''}"
    if kind == "tool":
        tool = _chat_tool_call(payload, run=run)
        detail = _chat_tool_call_display(tool)
        error = _text(payload.get("error"))
        if error:
            return _chat_dim(f"{marker} ran {detail} failed: {_chat_summarize(error, width=120)}")
        return _chat_dim(f"{marker} ran {detail}")
    if kind == "run":
        target = executable_label(
            _text(step_payload.get("target_kind")) or "run",
            _text(step_payload.get("target")),
            metadata=_mapping(step_payload.get("metadata")),
        ).replace(":", " ", 1)
        return _chat_dim(f"{marker} ran {target}")
    if kind in {"step", "parallel", "bind"}:
        op = _text(step_payload.get("op")) or "flow"
        return _chat_dim(f"{marker} ran {op}")
    if kind in {"system", "error"}:
        return _chat_system_line(payload)
    return _chat_dim(f"{marker} ran {kind or 'step'}")


def _chat_record_system_event(run: _ChatRun, message: str, *, clear_active: bool) -> None:
    if clear_active:
        run.steps.clear()
    index = max(run.step_indexes(), default=0) + 1
    kind = "error" if message.startswith("error:") else "system"
    if kind == "error":
        message = message.removeprefix("error:").strip()
    run.completed_steps[index] = {
        "kind": kind,
        "step_index": index,
        "payload": {"message": message},
    }


def _chat_stopped_run_message(status: str, error: str | None) -> str:
    if error:
        return _chat_friendly_error(error)
    if status in {"canceled", "cancelled"}:
        return "canceled"
    if status in {"failed", "error"}:
        return "failed"
    return status


def _chat_friendly_error(message: str) -> str:
    text = message.strip()
    if text.startswith("runtime request failed:"):
        text = text.removeprefix("runtime request failed:").strip()
    extracted = _chat_extract_error_message(text)
    if extracted:
        return extracted
    return text


def _chat_extract_error_message(text: str) -> str | None:
    candidates = [text]
    if " - " in text:
        candidates.append(text.split(" - ", 1)[1].strip())
    for candidate in candidates:
        parsed = _chat_parse_error_payload(candidate)
        if parsed is None:
            continue
        error = parsed.get("error")
        if isinstance(error, Mapping):
            message = _text(error.get("message"))
            if message is not None:
                return message
        message = _text(parsed.get("message"))
        if message is not None:
            return message
    return None


def _chat_parse_error_payload(text: str) -> Mapping[str, Any] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            return None
    return cast(Mapping[str, Any], parsed) if isinstance(parsed, Mapping) else None


def _chat_marker_for(kind: str | None) -> str:
    return {
        "model": "•",
        "tool": "›",
        "run": "›",
        "step": "─",
        "parallel": "⋯",
        "bind": "→",
        "system": "◇",
        "error": "!",
    }.get(kind or "", "·")


def _chat_tool_name(payload: Mapping[str, Any], *, run: _ChatRun | None = None) -> str:
    return _chat_tool_call(payload, run=run).name


def _chat_tool_call(payload: Mapping[str, Any], *, run: _ChatRun | None = None) -> _ChatToolCall:
    output_tool: _ChatToolCall | None = None
    for part in _list(payload.get("output")):
        if not isinstance(part, Mapping):
            continue
        name = _text(part.get("tool_name")) or _text(part.get("tool_family"))
        if name is not None:
            tool_input = part.get("input")
            output_tool = _ChatToolCall(name=name, input=dict(tool_input) if isinstance(tool_input, Mapping) else {})
            if output_tool.input:
                return output_tool
    for part in _list(payload.get("input")):
        if not isinstance(part, Mapping):
            continue
        name = _text(part.get("tool_name")) or _text(part.get("tool_family"))
        if name is not None:
            tool_input = part.get("input")
            return _ChatToolCall(name=name, input=dict(tool_input) if isinstance(tool_input, Mapping) else {})
        if run is None:
            continue
        ref_step = _int_or_none(part.get("step_index"))
        ref_part = _int_or_none(part.get("part_index"))
        if ref_step is not None and ref_part is not None:
            tool = run.tool_calls_by_part.get((ref_step, ref_part))
            if tool is not None:
                return tool
    if output_tool is not None:
        return output_tool
    step_payload = _mapping(payload.get("payload"))
    name = _text(step_payload.get("tool_name")) or _text(step_payload.get("tool")) or _text(step_payload.get("name"))
    tool_input = step_payload.get("input")
    return _ChatToolCall(name=name or "tool", input=dict(tool_input) if isinstance(tool_input, Mapping) else {})


def _chat_tool_call_display(tool: _ChatToolCall) -> str:
    input_summary = _chat_tool_input_summary(tool.input)
    if input_summary:
        return f"{tool.name}: {input_summary}"
    return tool.name


def _chat_model_tool_request_summary(payload: Mapping[str, Any], *, run: _ChatRun | None = None) -> str:
    tools: list[str] = []
    for part in _list(payload.get("output")):
        if not isinstance(part, Mapping) or part.get("type") != "tool_call":
            continue
        name = _text(part.get("tool_name")) or _text(part.get("tool_family")) or "tool"
        tool_input = part.get("input")
        tool = _ChatToolCall(name=name, input=dict(tool_input) if isinstance(tool_input, Mapping) else {})
        tools.append(_chat_tool_call_display(tool))
    if not tools and run is not None:
        step_index = _chat_step_index(payload)
        tools.extend(
            _chat_tool_call_display(tool)
            for (tool_step_index, _part_index), tool in sorted(run.tool_calls_by_part.items())
            if tool_step_index == step_index
        )
    return "; ".join(tools)


def _chat_tool_input_summary(tool_input: Mapping[str, Any]) -> str:
    if not tool_input:
        return ""
    for key in ("command", "cmd", "query", "path", "url", "prompt", "text"):
        value = tool_input.get(key)
        if value is not None:
            return _chat_plain_value(value)
    if len(tool_input) == 1:
        value = next(iter(tool_input.values()))
        return _chat_plain_value(value)
    pieces = [f"{key}={_chat_plain_value(value)}" for key, value in tool_input.items()]
    return ", ".join(pieces)


def _chat_plain_value(value: object) -> str:
    if isinstance(value, str):
        return _chat_summarize(value, width=160)
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    return _chat_summarize(json.dumps(value, ensure_ascii=False, separators=(",", ":")), width=160)


def _chat_system_line(payload: Mapping[str, Any]) -> str:
    kind = str(payload.get("kind") or "system")
    step_payload = _mapping(payload.get("payload"))
    message = (
        _text(payload.get("error"))
        or _text(step_payload.get("message"))
        or _text(step_payload.get("op"))
        or _text(step_payload.get("status"))
        or "runtime event"
    )
    marker = _chat_marker_for(kind)
    line = f"{marker} {message}"
    if kind in {"error", "system"}:
        return line
    return _chat_dim(line)


def _chat_dim(text: str) -> str:
    return f"{_CHAT_DIM}{text}{_CHAT_NORMAL_INTENSITY}"


def _chat_panel_user_block(run: _ChatRun) -> list[str]:
    return _chat_input_bar_plain_lines(_chat_run_input_bar_spec(run))


def _chat_scrollback_user_block(run: _ChatRun) -> list[str]:
    return _chat_input_bar_ansi_lines(_chat_run_input_bar_spec(run))


def _chat_run_input_bar_spec(run: _ChatRun) -> _ChatInputBarSpec:
    footer = _chat_run_input_footer(run)
    return _ChatInputBarSpec(
        kind="normal",
        marker=">",
        text=run.message,
        footer=footer,
        footer_dim=bool(footer),
    )


def _chat_local_input_bar_spec(message: str) -> _ChatInputBarSpec:
    return _ChatInputBarSpec(kind="normal", marker=">", text=message)


def _chat_run_input_footer(run: _ChatRun) -> str:
    queue_footer = _chat_queue_activity_line(run)
    if queue_footer:
        return f"  {queue_footer}"
    if run.run_id:
        return f"  {run.run_id}"
    return ""


def _chat_input_block_line(content: str) -> str:
    return _chat_input_bar_ansi_line(
        _ChatInputBarRow((_ChatInputBarSegment(content),)),
        kind="normal",
    )


def _chat_input_bar_ansi_lines(spec: _ChatInputBarSpec) -> list[str]:
    return [_chat_input_bar_ansi_line(row, kind=spec.kind) for row in _chat_input_bar_rows(spec)]


def _chat_input_bar_plain_lines(spec: _ChatInputBarSpec) -> list[str]:
    return [_chat_input_bar_plain_line(row) for row in _chat_input_bar_rows(spec)]


def _chat_input_bar_plain_line(row: _ChatInputBarRow) -> str:
    return "".join(segment.text for segment in row.segments) if row.bar else ""


def _chat_input_bar_ansi_line(row: _ChatInputBarRow, *, kind: Literal["normal", "steer"]) -> str:
    if not row.bar:
        return ""
    fg, bg = _chat_input_bar_colors(kind)
    content = "".join(_chat_dim(segment.text) if segment.dim else segment.text for segment in row.segments)
    return f"{_chat_ansi_style(fg, bg)}{_chat_pad_visible(content, _chat_terminal_width())}{_CHAT_RESET}"


def _chat_input_bar_fragments(spec: _ChatInputBarSpec) -> list[tuple[str, str]]:
    return _chat_join_fragment_rows(_chat_input_bar_fragment_rows(spec))


def _chat_input_bar_fragment_rows(spec: _ChatInputBarSpec) -> list[list[tuple[str, str]]]:
    return [_chat_input_bar_line_fragments(row, kind=spec.kind) for row in _chat_input_bar_rows(spec)]


def _chat_input_bar_rows(spec: _ChatInputBarSpec) -> list[_ChatInputBarRow]:
    rows: list[_ChatInputBarRow] = []
    if spec.outer_blank:
        rows.append(_ChatInputBarRow(bar=False))
    rows.append(_ChatInputBarRow())
    for index, line in enumerate(spec.text.splitlines() or [""]):
        if index == 0:
            rows.append(
                _ChatInputBarRow(
                    (
                        _ChatInputBarSegment(spec.marker, dim=True),
                        _ChatInputBarSegment(f" {line}"),
                    )
                )
            )
        else:
            rows.append(_ChatInputBarRow((_ChatInputBarSegment(f"  {line}"),)))
    footer = (_ChatInputBarSegment(spec.footer, dim=spec.footer_dim),) if spec.footer else ()
    rows.append(_ChatInputBarRow(footer))
    if spec.outer_blank:
        rows.append(_ChatInputBarRow(bar=False))
    return rows


def _chat_input_bar_line_fragments(
    row: _ChatInputBarRow, *, kind: Literal["normal", "steer"]
) -> list[tuple[str, str]]:
    if not row.bar:
        return []
    fragments: list[tuple[str, str]] = []
    visible_len = 0
    for segment in row.segments:
        if not segment.text:
            continue
        fragments.append((_chat_input_bar_class(kind, dim=segment.dim), segment.text))
        visible_len += _chat_display_len(segment.text)
    padding = " " * max(0, _chat_terminal_width() - visible_len)
    if padding:
        fragments.append((_chat_input_bar_class(kind, dim=False), padding))
    return fragments


def _chat_input_bar_colors(kind: Literal["normal", "steer"]) -> tuple[str, str]:
    if kind == "steer":
        return _CHAT_STEER_INPUT_FG, _CHAT_STEER_INPUT_BG
    return _CHAT_INPUT_FG, _CHAT_INPUT_BG


def _chat_input_bar_class(kind: Literal["normal", "steer"], *, dim: bool) -> str:
    prefix = "normal-input" if kind == "normal" else "steer-input"
    return f"class:{prefix}.dim" if dim else f"class:{prefix}"


def _chat_join_fragment_rows(rows: Sequence[Sequence[tuple[str, str]]]) -> list[tuple[str, str]]:
    fragments: list[tuple[str, str]] = []
    for index, row in enumerate(rows):
        fragments.extend(row)
        if index < len(rows) - 1:
            fragments.append(("", "\n"))
    return fragments


def _chat_pad_visible(content: str, width: int) -> str:
    return content + " " * max(0, width - _chat_display_len(content))


def _chat_terminal_width(default: int = 100) -> int:
    return shutil.get_terminal_size((default, 24)).columns


def _chat_ui_palette() -> dict[str, str]:
    return {
        "": "",
        "queue": _chat_prompt_style(_CHAT_QUEUE_FG, _CHAT_QUEUE_BG),
        "queue.dim": _chat_prompt_style(_CHAT_QUEUE_DIM_FG, _CHAT_QUEUE_BG),
        "normal-input": _chat_prompt_style(_CHAT_INPUT_FG, _CHAT_INPUT_BG),
        "normal-input.dim": _chat_prompt_style(_CHAT_INPUT_DIM_FG, _CHAT_INPUT_BG),
        "input": _chat_prompt_style(_CHAT_INPUT_FG, _CHAT_INPUT_BG),
        "steer-input": _chat_prompt_style(_CHAT_STEER_INPUT_FG, _CHAT_STEER_INPUT_BG),
        "steer-input.dim": _chat_prompt_style(_CHAT_STEER_INPUT_DIM_FG, _CHAT_STEER_INPUT_BG),
        "cursor": _chat_prompt_style(_CHAT_CURSOR_FG, _CHAT_CURSOR_BG),
        "input.cursor": _chat_prompt_style(_CHAT_CURSOR_FG, _CHAT_CURSOR_BG),
        "status": _chat_prompt_style(_CHAT_STATUS_FG, _CHAT_STATUS_BG),
        "status.model": "fg:#ffd866",
        "status.thunk": "fg:#8fd7ff",
        "status.flow": "fg:#d7b3ff",
        "status.text": "fg:ansigray",
        "status.error": "fg:ansired",
    }


def _chat_prompt_style(fg: str, bg: str) -> str:
    return f"fg:{fg} bg:{bg}"


def _chat_ansi_style(fg: str, bg: str) -> str:
    if fg.startswith("#") or bg.startswith("#"):
        return f"\x1b[{_chat_sgr_color(fg, foreground=True)};{_chat_sgr_color(bg, foreground=False)}m"
    foreground = {
        "ansiblack": "30",
        "ansired": "31",
        "ansigreen": "32",
        "ansiyellow": "33",
        "ansiblue": "34",
        "ansimagenta": "35",
        "ansicyan": "36",
        "ansiwhite": "37",
        "ansibrightblack": "90",
        "ansibrightred": "91",
        "ansibrightgreen": "92",
        "ansibrightyellow": "93",
        "ansibrightblue": "94",
        "ansibrightmagenta": "95",
        "ansibrightcyan": "96",
        "ansibrightwhite": "97",
    }
    background = {
        "ansiblack": "40",
        "ansired": "41",
        "ansigreen": "42",
        "ansiyellow": "43",
        "ansiblue": "44",
        "ansimagenta": "45",
        "ansicyan": "46",
        "ansiwhite": "47",
        "ansibrightblack": "100",
        "ansibrightred": "101",
        "ansibrightgreen": "102",
        "ansibrightyellow": "103",
        "ansibrightblue": "104",
        "ansibrightmagenta": "105",
        "ansibrightcyan": "106",
        "ansibrightwhite": "107",
    }
    return f"\x1b[{foreground[fg]};{background[bg]}m"


def _chat_sgr_color(color: str, *, foreground: bool) -> str:
    if not color.startswith("#") or len(color) != 7:
        raise ValueError(f"unsupported color: {color}")
    red = int(color[1:3], 16)
    green = int(color[3:5], 16)
    blue = int(color[5:7], 16)
    prefix = "38" if foreground else "48"
    return f"{prefix};2;{red};{green};{blue}"


def _chat_run_lines(run: _ChatRun, *, include_steps: bool) -> list[str]:
    lines = [*_chat_scrollback_user_block(run), ""]
    if include_steps:
        lines.extend(_chat_run_activity_lines(run, _chat_completed_line_for))
    while lines and lines[-1] == "":
        lines.pop()
    lines.append("")
    return lines


def _chat_completed_line_for(run: _ChatRun, index: int) -> str:
    if index in run.completed_steps:
        return _chat_completed_step_line(run.completed_steps[index], run=run)
    return _chat_active_step_line(run.steps[index])


def _chat_active_activity_fragment_rows(
    run: _ChatRun,
    step_renderer: Callable[[_ChatRun, int], str],
) -> list[list[tuple[str, str]]]:
    state_line = _chat_run_state_line(run)
    queue_line = _chat_queue_activity_line(run)
    if queue_line or _chat_flow_stage_lines(run):
        return [_chat_line_fragments(line) for line in _chat_run_activity_lines(run, step_renderer)]

    rows: list[list[tuple[str, str]]] = []
    if state_line and not _chat_state_line_after_activity(run):
        rows.append(_chat_line_fragments(state_line))
    timeline = run.timeline or [("step", index) for index in run.step_indexes()]
    rendered_steps: set[int] = set()
    for position, (kind, index) in enumerate(timeline):
        if kind == "command":
            command = run.commands.get(index)
            if command is not None:
                rows.extend(_chat_command_activity_fragment_rows(run, command.payload, position))
            continue
        if index in rendered_steps:
            continue
        step_lines = _chat_step_activity_lines(run, index, step_renderer)
        if step_lines:
            rows.extend(_chat_line_fragments(line) for line in step_lines)
            rendered_steps.add(index)
    for index in run.step_indexes():
        if index not in rendered_steps:
            rows.extend(_chat_line_fragments(line) for line in _chat_step_activity_lines(run, index, step_renderer))
    if state_line and _chat_state_line_after_activity(run):
        rows.append(_chat_line_fragments(state_line))
    return rows


def _chat_line_fragments(line: str) -> list[tuple[str, str]]:
    return cast(list[tuple[str, str]], to_formatted_text(ANSI(line)))


def _chat_run_activity_lines(run: _ChatRun, step_renderer: Callable[[_ChatRun, int], str]) -> list[str]:
    state_line = _chat_run_state_line(run)
    queue_line = _chat_queue_activity_line(run)
    if queue_line:
        return [*_chat_terminal_event_lines(run, step_renderer), *_chat_run_result_lines(run)]
    flow_lines = _chat_flow_stage_lines(run)
    if flow_lines:
        lines = []
        if state_line and not _chat_state_line_after_activity(run):
            lines.append(state_line)
        lines.extend(flow_lines)
        lines.extend(_chat_timeline_command_lines(run))
        lines.extend(_chat_terminal_event_lines(run, step_renderer))
        if state_line and _chat_state_line_after_activity(run):
            lines.append(state_line)
        lines.extend(_chat_run_result_lines(run))
        return lines
    lines: list[str] = []
    if state_line and not _chat_state_line_after_activity(run):
        lines.append(state_line)
    timeline = run.timeline or [("step", index) for index in run.step_indexes()]
    rendered_steps: set[int] = set()
    for position, (kind, index) in enumerate(timeline):
        if kind == "command":
            command = run.commands.get(index)
            if command is not None:
                lines.extend(_chat_command_activity_lines(run, command.payload, position))
            continue
        if index in rendered_steps:
            continue
        step_lines = _chat_step_activity_lines(run, index, step_renderer)
        if step_lines:
            lines.extend(step_lines)
            rendered_steps.add(index)
    for index in run.step_indexes():
        if index not in rendered_steps:
            lines.extend(_chat_step_activity_lines(run, index, step_renderer))
    if state_line and _chat_state_line_after_activity(run):
        lines.append(state_line)
    lines.extend(_chat_run_result_lines(run))
    return lines


def _chat_state_line_after_activity(run: _ChatRun) -> bool:
    status = _chat_run_display_status(run.status)
    return status in {"succeeded", "finished", "completed", "done", "failed", "error", "canceled", "cancelled"}


def _chat_step_activity_lines(
    run: _ChatRun,
    index: int,
    step_renderer: Callable[[_ChatRun, int], str],
) -> list[str]:
    if index in run.steps:
        return [step_renderer(run, index)]
    payload = run.completed_steps.get(index)
    if payload is None:
        return []
    if _chat_is_terminal_event_payload(run, index, payload):
        return []
    if payload.get("kind") == "model":
        text = _event_parts_text(payload.get("output"))
        if text:
            return _chat_message_lines(_chat_marker_for("model"), text)
        if _chat_model_tool_requests_have_results(run, index):
            return []
    return [step_renderer(run, index)]


def _chat_timeline_command_lines(run: _ChatRun) -> list[str]:
    lines: list[str] = []
    for position, (kind, index) in enumerate(run.timeline):
        if kind != "command":
            continue
        command = run.commands.get(index)
        if command is None:
            continue
        lines.extend(_chat_command_activity_lines(run, command.payload, position))
    return lines


def _chat_command_activity_lines(run: _ChatRun, command: Mapping[str, Any], timeline_position: int) -> list[str]:
    if command.get("kind") == "steer":
        return _chat_steer_input_block(command, waiting=_chat_command_is_waiting(run, timeline_position))
    if command.get("kind") == "stop":
        return [] if _chat_run_is_stopped(run) else [_chat_dim("canceling...")]
    return []


def _chat_command_activity_fragment_rows(
    run: _ChatRun, command: Mapping[str, Any], timeline_position: int
) -> list[list[tuple[str, str]]]:
    if command.get("kind") == "steer":
        return _chat_steer_input_fragment_rows(command, waiting=_chat_command_is_waiting(run, timeline_position))
    if command.get("kind") == "stop":
        return [] if _chat_run_is_stopped(run) else [_chat_line_fragments(_chat_dim("canceling..."))]
    return []


def _chat_command_is_waiting(run: _ChatRun, timeline_position: int) -> bool:
    if not run.steps:
        return False
    return not any(kind == "step" for kind, _index in run.timeline[timeline_position + 1 :])


def _chat_steer_input_block(command: Mapping[str, Any], *, waiting: bool) -> list[str]:
    return _chat_input_bar_ansi_lines(_chat_steer_input_bar_spec(command, waiting=waiting))


def _chat_steer_input_fragment_rows(command: Mapping[str, Any], *, waiting: bool) -> list[list[tuple[str, str]]]:
    return _chat_input_bar_fragment_rows(_chat_steer_input_bar_spec(command, waiting=waiting))


def _chat_steer_input_bar_spec(command: Mapping[str, Any], *, waiting: bool) -> _ChatInputBarSpec:
    message = _event_message_text(command.get("message"))
    return _ChatInputBarSpec(
        kind="steer",
        marker="+",
        text=message,
        footer="  pending for next step" if waiting else "",
        footer_dim=waiting,
        outer_blank=True,
    )


def _chat_terminal_event_lines(run: _ChatRun, step_renderer: Callable[[_ChatRun, int], str]) -> list[str]:
    lines: list[str] = []
    for index in run.step_indexes():
        payload = run.completed_steps.get(index)
        if payload is None:
            continue
        if _chat_is_terminal_event_payload(run, index, payload):
            continue
        if payload.get("kind") not in {"error", "system"}:
            continue
        lines.append(step_renderer(run, index))
    return lines


def _chat_model_tool_requests_have_results(run: _ChatRun, model_step_index: int) -> bool:
    model_payload = run.completed_steps.get(model_step_index)
    if model_payload is None or not _chat_model_tool_request_summary(model_payload, run=run):
        return False
    for payload in run.completed_steps.values():
        if payload.get("kind") != "tool":
            continue
        for item in _list(payload.get("input")):
            if not isinstance(item, Mapping):
                continue
            if _int_or_none(item.get("step_index")) == model_step_index:
                return True
        if payload.get("step_index") == model_step_index:
            return True
    return False


def _chat_run_state_line(run: _ChatRun) -> str:
    status = _chat_run_display_status(run.status)
    if not status:
        return ""
    if status in {"queued", "waiting", "submitting"}:
        return ""
    if status == "running":
        return ""
    if status == "canceling":
        return _chat_dim("canceling...")
    return ""


def _chat_run_result_lines(run: _ChatRun) -> list[str]:
    run_id = run.run_id or "run"
    status = _chat_run_display_status(run.status)
    if status in {"", "queued", "waiting", "submitting", "running", "canceling", "succeeded", "finished", "completed", "done"}:
        return []
    if status in {"canceled", "cancelled"}:
        return [_chat_dim(_chat_result_divider_line(run_id, "canceled"))]
    if status in {"failed", "error"}:
        lines = [_chat_dim(_chat_result_divider_line(run_id, "failed"))]
        error = _chat_terminal_error(run)
        if error:
            lines.extend(_chat_dim(f"  {line}") for line in _chat_wrap_plain_lines(error))
        return lines
    return [_chat_dim(_chat_result_divider_line(run_id, status))]


def _chat_result_divider_line(run_id: str, status: str) -> str:
    return f"  ──────── {run_id} {status} ────────"


def _chat_run_is_stopped(run: _ChatRun) -> bool:
    return _chat_run_display_status(run.status) in {"succeeded", "finished", "completed", "done", "failed", "error", "canceled", "cancelled"}


def _chat_terminal_error(run: _ChatRun) -> str:
    if run.terminal_error:
        return run.terminal_error
    for index in sorted(run.completed_steps, reverse=True):
        payload = run.completed_steps[index]
        kind = str(payload.get("kind") or "")
        if kind not in {"error", "system"}:
            continue
        message = _chat_terminal_event_message(payload)
        if message and message not in {"failed", "canceled", "cancelled"}:
            return message
    return ""


def _chat_wrap_plain_lines(text: str) -> list[str]:
    width = max(shutil.get_terminal_size((100, 24)).columns - 2, 20)
    lines: list[str] = []
    for raw_line in text.splitlines() or [""]:
        line = raw_line.strip()
        if len(line) <= width:
            lines.append(line)
            continue
        while len(line) > width:
            split_at = line.rfind(" ", 0, width + 1)
            if split_at <= 0:
                split_at = width
            lines.append(line[:split_at].rstrip())
            line = line[split_at:].lstrip()
        lines.append(line)
    return [line for line in lines if line]


def _chat_terminal_event_message(payload: Mapping[str, Any]) -> str:
    step_payload = _mapping(payload.get("payload"))
    return (
        _text(payload.get("error"))
        or _text(step_payload.get("message"))
        or _text(step_payload.get("op"))
        or _text(step_payload.get("status"))
        or ""
    )


def _chat_is_terminal_event_payload(run: _ChatRun, index: int, payload: Mapping[str, Any]) -> bool:
    if _chat_run_display_status(run.status) not in {"failed", "error", "canceled", "cancelled"}:
        return False
    if index != max(run.step_indexes(), default=index):
        return False
    return payload.get("kind") in {"error", "system"}


def _chat_progress_tail(line: str) -> str:
    visible = _chat_visible_text(line).rstrip()
    if visible.endswith("..."):
        return line
    return f"{line}..."


def _chat_run_display_status(status: str) -> str:
    if status == "finished":
        return "succeeded"
    return status.strip().lower()


def _chat_queue_activity_line(run: _ChatRun) -> str:
    if run.queue_state == "waiting":
        reason = run.waiting_reason or "queue"
        suffix = f" · position {run.queue_position}" if run.queue_position else ""
        run_id = f" {run.run_id}" if run.run_id else ""
        return f"waiting{run_id} for {reason}{suffix}"
    if run.status == "submitting":
        return "submitting"
    return ""


def _chat_flow_stage_lines(run: _ChatRun) -> list[str]:
    stages, calls = _chat_flow_projection(run)
    if not stages:
        return []
    lines: list[str] = []
    for stage in stages:
        lines.append(_chat_flow_stage_line(stage, calls))
        lines.extend(_chat_flow_stage_detail_lines(stage, calls, child_run_for=lambda call: run.child_run(call.run_id)))
        lines.append("")
    return lines


def _chat_flow_projection(run: _ChatRun) -> tuple[list[FlowStageView], dict[str, FlowCallView]]:
    steps: list[Mapping[str, Any]] = []
    for step in run.step_indexes():
        payload = run.completed_steps.get(step)
        if payload is not None:
            steps.append(payload)
            continue
        active = run.steps.get(step)
        if active is None or active.kind not in {"step", "parallel", "bind", "run"}:
            continue
        active_payload = dict(active.payload)
        if "payload" not in active_payload:
            active_payload["payload"] = dict(_mapping(active_payload.get("metadata")))
        steps.append(active_payload)
    return project_flow_from_step_payloads(steps)


def _chat_flow_stage_line(stage: FlowStageView, calls: Mapping[str, FlowCallView]) -> str:
    pieces = [stage_title_label(stage)]
    tail = _chat_flow_stage_tail(stage, calls)
    if tail:
        pieces.append(tail)
    return " · ".join(pieces)


def _chat_flow_stage_tail(stage: FlowStageView, calls: Mapping[str, FlowCallView]) -> str:
    stage_call_items = stage_calls(stage, calls)
    total = len(stage_call_items)
    failed = sum(1 for call in stage_call_items if call.status == "failed")
    done = sum(1 for call in stage_call_items if call.status in {"succeeded", "done", "failed", "canceled"})
    parts: list[str] = []
    if stage.output_shape:
        parts.append(f"{stage.input_shape or '?'} -> {stage.output_shape or '?'}")
    elif stage.item_total is not None and total:
        parts.append(f"{done}/{stage.item_total} calls")
    elif total:
        parts.append(f"{done}/{total} calls")
    if failed:
        parts.append(f"{failed} failed")
    if stage.parallelism and stage.parallelism > 1:
        parts.append(f"{stage.parallelism} lanes")
    return " · ".join(parts)


def _chat_flow_stage_detail_lines(
    stage: FlowStageView,
    calls: Mapping[str, FlowCallView],
    *,
    child_run_for: Callable[[FlowCallView], object | None],
) -> list[str]:
    stage_call_items = stage_calls(stage, calls)
    if not stage_call_items:
        return []
    if stage.parallelism and stage.parallelism > 1:
        lines: list[str] = []
        lanes = stage_lanes(stage_call_items)
        for lane_index in range(stage.parallelism):
            lane_calls = lanes.get(lane_index, [])
            if not lane_calls:
                continue
            done = sum(1 for call in lane_calls if call.status in {"succeeded", "done", "failed", "canceled"})
            lines.append(
                _chat_dim(
                    f"{_CHAT_FLOW_DETAIL_INDENT}{_CHAT_FLOW_STATEMENT_MARKER} "
                    f"lane {lane_index + 1}/{stage.parallelism} · {done}/{len(lane_calls)} calls"
                )
            )
            for call in lane_calls:
                lines.extend(_chat_flow_call_lines(call, child_run_for(call), indent=_CHAT_FLOW_DETAIL_INDENT))
        return lines
    lines = []
    for call in stage_call_items:
        lines.extend(_chat_flow_call_lines(call, child_run_for(call), indent=_CHAT_FLOW_DETAIL_INDENT))
    return lines


def _chat_flow_call_lines(call: FlowCallView, child_run: object | None, *, indent: str) -> list[str]:
    header = _chat_dim(f"{indent}{_CHAT_FLOW_STATEMENT_MARKER} {_chat_flow_call_label(call, child_run)}")
    lines = [header]
    step_lines = _chat_child_run_step_lines(child_run, indent=indent)
    lines.extend(step_lines)
    return lines


def _chat_flow_call_label(call: FlowCallView, child_run: object | None) -> str:
    if isinstance(child_run, _ChatRun):
        status = child_run.status or call.status
        target = (
            executable_label(child_run.executable_kind, child_run.executable_name).replace(":", " ", 1)
            if child_run.executable_name
            else call.label
        )
        return f"{call.label if call.item_index is not None else target} · {call.run_id} {status}"
    if isinstance(child_run, Mapping):
        child_mapping = cast(Mapping[str, Any], child_run)
        info = _mapping(child_mapping.get("info"))
        output = _mapping(child_mapping.get("output"))
        status = _display_run_status(output.get("status")) or call.status
        name = _text(info.get("executable_name"))
        target = (
            executable_label(
                _text(info.get("executable_kind")) or "run",
                name,
                metadata=_mapping(info.get("metadata")),
            ).replace(":", " ", 1)
            if name
            else call.label
        )
        return f"{call.label if call.item_index is not None else target} · {call.run_id} {status}"
    return f"{call.label} · {call.run_id} {call.status}"


def _chat_child_run_step_lines(child_run: object | None, *, indent: str) -> list[str]:
    if isinstance(child_run, _ChatRun):
        return _chat_child_chat_run_step_lines(child_run, indent=indent)
    if isinstance(child_run, Mapping):
        return _chat_child_mapping_run_step_lines(cast(Mapping[str, Any], child_run), indent=indent)
    return []


def _chat_child_chat_run_step_lines(run: _ChatRun, *, indent: str) -> list[str]:
    lines: list[str] = []
    if run.executable_kind == "flow":
        for line in _chat_flow_stage_lines(run):
            lines.append(f"{indent}{line}" if line else "")
        return lines
    for index in run.step_indexes():
        if index in run.steps:
            lines.append(f"{indent}{_chat_active_step_line(run.steps[index])}")
            continue
        payload = run.completed_steps[index]
        lines.extend(_chat_child_completed_step_lines(payload, run=run, indent=indent))
    return lines


def _chat_child_mapping_run_step_lines(run: Mapping[str, Any], *, indent: str) -> list[str]:
    info = _mapping(run.get("info"))
    if _text(info.get("executable_kind")) == "flow":
        stages, calls = project_flow_from_run(run)
        lines: list[str] = []
        for stage in stages:
            lines.append(f"{indent}{_chat_flow_stage_line(stage, calls)}")
            lines.append("")
        return lines
    lines: list[str] = []
    for step in _run_steps(run):
        record = _mapping(step.get("record"))
        lines.extend(_chat_child_completed_step_lines(record, run=None, indent=indent))
    return lines


def _chat_child_completed_step_lines(
    payload: Mapping[str, Any],
    *,
    run: _ChatRun | None,
    indent: str,
) -> list[str]:
    kind = _text(payload.get("kind"))
    if kind == "model":
        text = _event_parts_text(payload.get("output"))
        lines: list[str] = []
        if text:
            lines.extend(f"{indent}{line}" for line in _chat_message_lines(_chat_marker_for("model"), text))
        requests = _chat_model_tool_request_summary(payload, run=run)
        if requests:
            lines.append(f"{indent}{_chat_marker_for('model')} requested {requests}")
        if lines:
            return lines
    line = f"{indent}{_chat_completed_step_line(payload, run=run)}"
    if kind != "tool":
        return [line]
    return [line, *_chat_tool_message_lines(payload, indent=indent)]


def _chat_tool_message_lines(payload: Mapping[str, Any], *, indent: str) -> list[str]:
    text = _chat_tool_message_text(payload)
    if not text:
        return []
    prefix = f"{indent}  "
    width = max(8, _chat_markdown_width() - _chat_display_len(prefix))
    message = _chat_truncate_display(" ".join(_chat_visible_text(text).split()), width=width)
    return [_chat_dim(f"{prefix}{message}")]


def _chat_tool_message_text(payload: Mapping[str, Any]) -> str:
    messages: list[str] = []
    for part in _list(payload.get("output")):
        if not isinstance(part, Mapping) or part.get("type") != "tool_result":
            continue
        output = part.get("output")
        if isinstance(output, str):
            messages.append(output.strip())
            continue
        if isinstance(output, Mapping):
            stdout = _text(output.get("stdout"))
            stderr = _text(output.get("stderr"))
            if stdout:
                messages.append(stdout)
            if stderr:
                messages.append(stderr)
            if stdout or stderr:
                continue
        if output is not None:
            messages.append(_chat_plain_value(output))
    return "\n".join(item for item in messages if item).strip()


def _chat_assistant_lines(run: _ChatRun) -> list[str]:
    lines: list[str] = []
    for index in run.step_indexes():
        payload = run.completed_steps.get(index)
        if payload is None or payload.get("kind") != "model":
            continue
        text = _event_parts_text(payload.get("output"))
        if not text:
            continue
        lines.extend(_chat_message_lines(_chat_marker_for("model"), text))
    return lines


def _chat_message_lines(marker: str, text: str) -> list[str]:
    source_lines = _chat_render_markdown_lines(text)
    source_lines = source_lines or [""]
    lines = [f"{marker} {source_lines[0]}"]
    lines.extend(f"  {line}" for line in source_lines[1:])
    return lines


def _chat_local_command(message: str) -> tuple[str, str] | None:
    stripped = message.strip()
    if not stripped.startswith("/"):
        return None
    command, _, argument = stripped[1:].partition(" ")
    if not command:
        return None
    return command, argument.strip()


def _chat_model_command_selectors(argument: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(split_model_selectors((argument,))))


def _chat_initial_model_label(selector_payload: Mapping[str, object]) -> str:
    requested = _chat_requested_model_selectors(selector_payload)
    return ", ".join(requested) if requested else "runtime model"


def _chat_resolved_model_label(
    ctx: typer.Context,
    selector_payload: Mapping[str, object],
    *,
    deps: ChatTuiDependencies,
) -> str:
    requested = _chat_requested_model_selectors(selector_payload)
    fallback = _chat_initial_model_label(selector_payload)
    try:
        payload = deps.runtime_json(ctx, "/api/v1/chat/models")
    except Exception:
        return fallback
    items = [_mapping(item) for item in _list(payload.get("items"))]
    if requested:
        labels = [
            _chat_model_item_label(item) if item is not None else selector
            for selector in requested
            for item in (_chat_find_model_item(items, selector),)
        ]
        return ", ".join(label for label in labels if label) or fallback
    default_selector = _text(payload.get("default"))
    if default_selector is not None:
        item = _chat_find_model_item(items, default_selector)
        if item is not None:
            return _chat_model_item_label(item)
        return default_selector
    if items:
        return _chat_model_item_label(items[0])
    return fallback


def _chat_resolve_model_command_labels(
    ctx: typer.Context,
    selectors: Sequence[str],
    *,
    deps: ChatTuiDependencies,
) -> tuple[str, ...] | None:
    try:
        payload = deps.runtime_json(ctx, "/api/v1/chat/models")
    except click.ClickException:
        return None
    items = [_mapping(item) for item in _list(payload.get("items"))]
    labels: list[str] = []
    for selector in selectors:
        item = _chat_find_model_item(items, selector)
        if item is None:
            return None
        labels.append(_chat_model_item_label(item))
    return tuple(labels)


def _chat_requested_model_selectors(selector_payload: Mapping[str, object]) -> tuple[str, ...]:
    models = selector_payload.get("models")
    if not isinstance(models, Sequence) or isinstance(models, (str, bytes, bytearray)):
        return ()
    return tuple(str(item) for item in models if str(item))


def _chat_find_model_item(items: Sequence[Mapping[str, Any]], selector: str) -> Mapping[str, Any] | None:
    normalized = _chat_model_selector_key(selector)
    for item in items:
        values = (
            _text(item.get("selector")),
            _text(item.get("ref")),
            _text(item.get("name")),
            _text(item.get("model")),
            _text(item.get("provider")),
        )
        if any(_chat_model_selector_key(value) == normalized for value in values if value is not None):
            return item
    return None


def _chat_model_selector_key(selector: str) -> str:
    return selector.strip().removeprefix("[").removesuffix("]")


def _chat_model_item_label(item: Mapping[str, Any]) -> str:
    ref = _text(item.get("ref"))
    if ref is not None:
        return ref
    provider = _text(item.get("provider"))
    model = _text(item.get("model"))
    if provider is not None and model is not None:
        return f"{provider}/{model}"
    return _text(item.get("selector")) or _text(item.get("name")) or "runtime model"


def _chat_set_executable_selector(selector_payload: dict[str, object], *, kind: str, name: str) -> None:
    selector_payload[kind] = name.strip()
    if kind == "thunk":
        selector_payload.pop("flow", None)
    elif kind == "flow":
        selector_payload.pop("thunk", None)


def _chat_executable_status_label(selector_payload: Mapping[str, object]) -> str:
    flow = _text(selector_payload.get("flow"))
    if flow:
        return f"flow:{flow}"
    thunk = _text(selector_payload.get("thunk"))
    if thunk:
        return f"thunk:{thunk}"
    return ""


def _chat_status_segments(label: str) -> list[tuple[str, str]]:
    pieces = [piece for piece in label.split("  ") if piece]
    if not pieces:
        return []
    segments: list[tuple[str, str]] = [("class:status.model", pieces[0])]
    for piece in pieces[1:]:
        if piece.startswith("thunk:"):
            segments.append(("class:status.text", "  "))
            segments.append(("class:status.thunk", piece))
        elif piece.startswith("flow:"):
            segments.append(("class:status.text", "  "))
            segments.append(("class:status.flow", piece))
        else:
            segments.append(("class:status.text", f"  {piece}"))
    return segments


def _chat_help_lines() -> list[str]:
    return [
        "Slash Commands",
        "",
        "/help, /?          Show help.",
        "/model [SELECTOR]  List or switch models.",
        "/thunk [NAME]      List or use a thunk.",
        "/flow [NAME]       List or use a flow.",
        "/queue             Show queue commands.",
        "/exit, /quit       Exit chat.",
    ]


def _chat_queue_help_lines() -> list[str]:
    return [
        "Queue Commands",
        "",
        "/queue steer N   Steer the active run with item #N.",
        "/queue edit N    Edit item #N in the input box.",
        "/queue delete N  Delete item #N.",
        "/queue clear     Clear all items.",
        "/q s N           First-letter abbreviations are accepted.",
    ]


def _chat_queue_command_index(value: str, item_count: int) -> int | None:
    index = _int_or_none(value)
    if index is None or index < 1 or index > item_count:
        return None
    return index - 1


def _chat_local_command_lines(message: str, body: Sequence[str]) -> list[str]:
    return [
        *_chat_scrollback_user_message_block(message),
        "",
        *_chat_system_block_lines(body),
        "",
    ]


def _chat_scrollback_user_message_block(message: str) -> list[str]:
    return _chat_input_bar_ansi_lines(_chat_local_input_bar_spec(message))


def _chat_system_block_lines(body: Sequence[str]) -> list[str]:
    if not body:
        return []
    first, *rest = body
    return [f"{_chat_marker_for('system')} {first}", *[f"  {line}" for line in rest]]


def _chat_model_list_lines(payload: Mapping[str, Any]) -> list[str]:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return ["No available chat models."]
    default = _text(payload.get("default"))
    lines: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        selector = _text(item.get("selector"))
        if selector is None:
            continue
        suffix = " default" if selector == default else ""
        detail = _chat_model_item_detail(item)
        lines.append(f"{selector}{suffix}{f'  {detail}' if detail else ''}")
    return lines or ["No available chat models."]


def _chat_executable_list_lines(payload: Mapping[str, Any], *, selected: str | None) -> list[str]:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return ["No available items."]
    default = _text(payload.get("default"))
    lines: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = _text(item.get("name"))
        if name is None:
            continue
        labels: list[str] = []
        if name == selected:
            labels.append("current")
        if name == default:
            labels.append("default")
        suffix = f"  {' '.join(labels)}" if labels else ""
        lines.append(f"{name}{suffix}")
    return lines or ["No available items."]


def _chat_model_item_detail(item: Mapping[str, Any]) -> str:
    pieces = [
        _text(item.get("provider")),
        _text(item.get("adapter")),
    ]
    return " ".join(piece for piece in pieces if piece)


def _chat_render_markdown_lines(text: str) -> list[str]:
    stream = io.StringIO()
    section_titles = _chat_markdown_section_titles(text)
    try:
        console = Console(
            file=stream,
            force_terminal=True,
            color_system="standard",
            width=_chat_markdown_width(),
            soft_wrap=False,
        )
        console.print(Markdown(text), width=_chat_markdown_width(), end="")
    except Exception:
        return text.splitlines()
    rendered = stream.getvalue().rstrip("\n")
    return _chat_compact_markdown_lines(rendered.splitlines(), section_titles=section_titles)


def _chat_compact_markdown_lines(lines: Sequence[str], *, section_titles: set[str]) -> list[str]:
    compact: list[str] = []
    normalized_lines = [line.rstrip() for line in lines]
    for index, normalized in enumerate(normalized_lines):
        visible = _chat_visible_text(normalized)
        if not visible.strip():
            if _chat_should_keep_markdown_blank(normalized_lines, index, section_titles=section_titles):
                if compact and compact[-1] != "":
                    compact.append("")
            continue
        compact.append(normalized)
    return compact


def _chat_should_keep_markdown_blank(lines: Sequence[str], index: int, *, section_titles: set[str]) -> bool:
    previous = _chat_previous_visible_line(lines, index)
    next_line = _chat_next_visible_line(lines, index)
    return _chat_is_section_title(previous, section_titles) or _chat_is_section_title(next_line, section_titles)


def _chat_previous_visible_line(lines: Sequence[str], index: int) -> str | None:
    for candidate in reversed(lines[:index]):
        visible = _chat_visible_text(candidate).strip()
        if visible:
            return visible
    return None


def _chat_next_visible_line(lines: Sequence[str], index: int) -> str | None:
    for candidate in lines[index + 1 :]:
        visible = _chat_visible_text(candidate).strip()
        if visible:
            return visible
    return None


def _chat_is_section_title(line: str | None, section_titles: set[str]) -> bool:
    return line is not None and line in section_titles


def _chat_markdown_section_titles(text: str) -> set[str]:
    titles: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        prefix, _, title = stripped.partition(" ")
        if 1 <= len(prefix) <= 6 and set(prefix) == {"#"} and title.strip():
            titles.add(title.strip())
    return titles


def _chat_markdown_width() -> int:
    return min(100, max(40, _chat_terminal_width() - 4))


def _chat_visible_text(text: str) -> str:
    visible: list[str] = []
    in_escape = False
    for char in text:
        if char == "\x1b":
            in_escape = True
        elif in_escape and char == "m":
            in_escape = False
        elif not in_escape:
            visible.append(char)
    return "".join(visible)


def _chat_header_lines(model_label: str, *, home_label: str) -> list[str]:
    content = [
        (
            f"{_CHAT_DIM}T··⅃ "
            f"{_CHAT_NORMAL_INTENSITY}{_CHAT_BOLD}Toolang{_CHAT_NORMAL_INTENSITY} "
            f"{_CHAT_DIM}(v{_toolang_version()}){_CHAT_NORMAL_INTENSITY}"
        ),
        "",
        f"model: {model_label}",
        f"home:  {home_label}",
    ]
    width = max(_chat_display_len(line) for line in content) + 2
    top = f"{_CHAT_DIM}╭{'─' * width}╮{_CHAT_NORMAL_INTENSITY}"
    bottom = f"{_CHAT_DIM}╰{'─' * width}╯{_CHAT_NORMAL_INTENSITY}"
    body = [
        f"{_CHAT_DIM}│{_CHAT_NORMAL_INTENSITY} {line}{' ' * (width - 1 - _chat_display_len(line))}{_CHAT_DIM}│{_CHAT_NORMAL_INTENSITY}"
        for line in content
    ]
    return [top, *body, bottom, " "]


def _chat_display_len(text: str) -> int:
    in_escape = False
    visible: list[str] = []
    for char in text:
        if char == "\x1b":
            in_escape = True
        elif in_escape and char == "m":
            in_escape = False
        elif not in_escape:
            visible.append(char)
    return get_cwidth("".join(visible))


def _chat_write_lines(lines: list[str], *, hide_cursor: bool = True) -> None:
    if hide_cursor:
        sys.stdout.write("\x1b[?25l")
    try:
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
    finally:
        if hide_cursor:
            sys.stdout.write("\x1b[?25h")
            sys.stdout.flush()


def _chat_summarize(message: str, *, width: int = 72) -> str:
    text = " ".join(message.split())
    if len(text) <= width:
        return text
    return f"{text[: width - 3].rstrip()}..."


def _chat_truncate_display(text: str, *, width: int) -> str:
    if width <= 0 or _chat_display_len(text) <= width:
        return text
    ellipsis = "..."
    if width <= len(ellipsis):
        return ellipsis[:width]
    limit = width - len(ellipsis)
    pieces: list[str] = []
    used = 0
    for char in text:
        char_width = get_cwidth(char)
        if used + char_width > limit:
            break
        pieces.append(char)
        used += char_width
    return f"{''.join(pieces).rstrip()}{ellipsis}"




def _toolang_version() -> str:
    return f"{_base_toolang_version()}{_source_state_suffix()}"


def _base_toolang_version() -> str:
    try:
        return package_version("toolang")
    except PackageNotFoundError:
        pyproject_path = Path(__file__).resolve().parents[3] / "pyproject.toml"
        try:
            data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return "unknown"
        project = data.get("project")
        if not isinstance(project, dict):
            return "unknown"
        version = project.get("version")
        return version if isinstance(version, str) else "unknown"


def _source_state_suffix() -> str:
    source_root = _source_tree_root()
    if source_root is None:
        return ""
    short_sha = _git_output(source_root, "rev-parse", "--short", "HEAD")
    if short_sha is None:
        return ""
    dirty = _git_output(source_root, "status", "--short")
    if dirty is None:
        return f"+{short_sha}"
    dirty_suffix = "*" if dirty else ""
    return f"+{short_sha}{dirty_suffix}"


def _git_output(source_root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=source_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _source_tree_root() -> Path | None:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / ".git").exists():
            return parent
    return None


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    if value is None:
        return None
    return None


def _display_run_status(status: object) -> str:
    text = str(status or "")
    return "succeeded" if text == "finished" else text


def _run_steps(run: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    output = _mapping(run.get("output"))
    return [_mapping(item) for item in _list(output.get("steps"))]


def _event_message_text(message: object) -> str:
    if not isinstance(message, Mapping):
        return ""
    typed_message = cast(Mapping[str, object], message)
    parts = typed_message.get("parts")
    if not isinstance(parts, list):
        return ""
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        typed_part = cast(Mapping[str, object], part)
        if typed_part.get("type") == "text":
            texts.append(str(typed_part.get("text") or ""))
    return "".join(texts).strip()


def _event_parts_text(parts: object) -> str:
    if not isinstance(parts, list):
        return ""
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        typed_part = cast(Mapping[str, object], part)
        if typed_part.get("type") == "text":
            texts.append(str(typed_part.get("text") or ""))
    return "".join(texts).strip()
