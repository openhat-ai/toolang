"""Terminal chat TUI state, layout, and rendering helpers."""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import shutil
import sys
import threading
from typing import Any, cast
from uuid import uuid4

import click
from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
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
from ..chat_history import ChatInputHistoryStore
from .chat_tui_bottom import (
    ChatBottomHooks,
    _ChatCommandBlock,
    _ChatFlowProjectionBlock,
    _ChatLastRunPanel,
    _ChatMutableBlock,
    _ChatPromptBox,
    _ChatQueueItem,
    _ChatRun,
    _ChatRunEndBlock,
    _ChatRunStartBlock,
    _ChatRunSteerBlock,
    _ChatStepBlock,
    _ChatSubmissionQueue,
    _ChatToolCall,
    _ChatTraceEventResult,
    _ChatUIEvent,
    _chat_run_end_block,
    _chat_step_block,
    configure_chat_bottom_hooks,
)
from .chat_tui_commands import (
    _chat_executable_list_lines,
    _chat_executable_status_label,
    _chat_help_lines,
    _chat_local_command,
    _chat_model_command_selectors,
    _chat_model_list_lines,
    _chat_queue_command_index,
    _chat_queue_help_lines,
    _chat_resolve_model_command_labels,
    _chat_resolved_model_label,
    _chat_set_executable_selector,
)
from .chat_tui_input import (
    _ChatInputBarRow,
    _ChatInputBarSegment,
    _ChatInputBarSpec,
    _chat_input_bar_ansi_line,
    _chat_input_bar_ansi_lines,
    _chat_input_bar_fragment_rows,
    _chat_input_bar_plain_lines,
)
from .chat_tui_env import _toolang_version
from .chat_tui_markdown import (
    _chat_markdown_width,
    _chat_render_markdown_lines,
)
from .chat_tui_theme import (
    _CHAT_BOLD,
    _CHAT_DIM,
    _CHAT_INPUT_BG as _CHAT_INPUT_BG,
    _CHAT_INPUT_FG as _CHAT_INPUT_FG,
    _CHAT_NORMAL_INTENSITY,
    _CHAT_RESET as _CHAT_RESET,
    _CHAT_STEER_INPUT_BG as _CHAT_STEER_INPUT_BG,
    _CHAT_STEER_INPUT_FG as _CHAT_STEER_INPUT_FG,
    _chat_ansi_style as _chat_ansi_style,
    _chat_dim,
    _chat_display_len,
    _chat_prompt_style as _chat_prompt_style,
    _chat_ui_palette,
    _chat_visible_text,
)
from .chat_tui_values import (
    _display_run_status,
    _event_message_text,
    _event_parts_text,
    _int_or_none,
    _list,
    _mapping,
    _run_steps,
    _text,
)


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
_CHAT_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_CHAT_FLOW_DETAIL_INDENT = "  "
_CHAT_FLOW_STATEMENT_MARKER = "‣"


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
            self.flush_queue_state(self.active_run)
            self.active_run.status = "error"
            self.active_run.terminal_error = friendly
            self.flush_pending_steer_blocks(self.active_run)
            _chat_record_system_event(self.active_run, f"error: {friendly}", clear_active=True)
            self.flush_terminal_event(self.active_run)
            self.flush_finalized_block(
                self.active_run,
                _chat_run_end_block(
                    {
                        "run_id": self.active_run.run_id,
                        "status": "failed",
                        "error": friendly,
                    },
                    run=self.active_run,
                ),
            )
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
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        if self.handle_child_trace_event(event_type, payload):
            self.app.invalidate()
            return
        if self.should_ignore_trace_event(event_type, payload):
            return
        run = self.run_for_trace_event(event_type, payload)
        if run is None:
            return
        result = run.apply_trace_event(event_type, payload)
        self.apply_trace_event_result(run, result)
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

    def run_for_trace_event(self, event_type: str, payload: Mapping[str, Any]) -> _ChatRun | None:
        if event_type == "run_starting":
            return self.ensure_starting_run(payload)
        if event_type == "run_waiting":
            if self.active_run is None:
                self.active_run = _ChatRun(
                    run_id=_text(payload.get("run_id")) or "",
                    message="",
                    status="queued",
                    accept_child_trace=True,
                )
            return self.active_run
        if event_type == "run_begin":
            return self.ensure_running_run(payload)
        return self.active_run

    def ensure_starting_run(self, payload: Mapping[str, Any]) -> _ChatRun:
        run_id = str(payload.get("run_id") or "")
        message = _event_message_text(payload.get("input"))
        if not message:
            message = self.active_run.message if self.active_run is not None else ""
        if self.active_run and (not self.active_run.run_id or self.active_run.run_id == run_id):
            self.active_run.run_id = run_id
            self.active_run.message = message
            return self.active_run
        self.active_run = _ChatRun(run_id=run_id, message=message, status="submitting", accept_child_trace=True)
        return self.active_run

    def ensure_running_run(self, payload: Mapping[str, Any]) -> _ChatRun:
        run_id = str(payload.get("run_id") or "")
        if self.active_run and (not self.active_run.run_id or self.active_run.run_id == run_id):
            self.active_run.run_id = run_id
            return self.active_run
        message = _event_message_text(payload.get("input"))
        self.active_run = _ChatRun(run_id=run_id, message=message, status="running", accept_child_trace=True)
        return self.active_run

    def apply_trace_event_result(self, run: _ChatRun, result: _ChatTraceEventResult) -> None:
        for action in result.scrollback:
            if action.kind == "queue_state":
                self.flush_queue_state(run)
            elif action.kind == "terminal_event":
                self.flush_terminal_event(run)
            elif action.kind == "block" and action.block is not None:
                self.flush_finalized_block(run, action.block)
        if result.send_cancel_request:
            self.maybe_send_cancel_request()
        if result.run_finished:
            self.active_run = None
            self.local_streaming.clear()
            self.prompt.clear_error()
            self.start_next_run()
            self.app.invalidate()

    def handle_child_trace_event(self, event_type: str, payload: Mapping[str, Any]) -> bool:
        if self.active_run is None or not self.active_run.accept_child_trace:
            return False
        run_id = _text(payload.get("run_id"))
        if event_type == "run_begin":
            parent_run_id = _text(payload.get("parent_run_id"))
            call_kind = _text(payload.get("call_kind")) or "top"
            if parent_run_id is None and call_kind == "top":
                return False
            root_run_id = _text(payload.get("root_run_id"))
            if parent_run_id == self.active_run.run_id or root_run_id == self.active_run.run_id or parent_run_id in self.active_run.child_runs:
                self.active_run.start_child_run(payload)
                child = self.active_run.child_run(run_id)
                if child is not None:
                    child.apply_trace_event(event_type, payload)
                return True
            return False
        child = self.active_run.child_run(run_id)
        if child is None:
            return False
        if event_type in {"run_waiting", "run_starting", "run_steering", "run_stopping", "run_begin", "step_begin", "part_begin", "part_delta", "part_end", "step_end", "run_end"}:
            child.apply_trace_event(event_type, payload)
            return True
        return False

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
            request_id = f"req_{uuid4().hex}"
            self.deps.runtime_post(
                self.ctx,
                f"/api/v1/runs/{run.run_id}/steer",
                payload={
                    "request_id": request_id,
                    "message": self.deps.message_payload(item.text),
                },
            )
        except click.ClickException as exc:
            self.prompt.set_error(_chat_friendly_error(exc.message))
            return True
        self.pending.pop(index)
        self.app.invalidate()
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

    def flush_queue_state(self, run: _ChatRun) -> None:
        line = _chat_queue_activity_line(run)
        if line:
            self.deps.write_lines([line])

    def flush_pending_steer_blocks(self, run: _ChatRun) -> None:
        for block in run.finalize_pending_steer_blocks():
            self.flush_finalized_block(run, block)

    def flush_finalized_block(self, run: _ChatRun, block: _ChatMutableBlock) -> None:
        if isinstance(block, _ChatRunStartBlock) and block.index in run.flushed_commands:
            return
        if isinstance(block, _ChatCommandBlock) and block.index in run.flushed_commands:
            return
        if isinstance(block, _ChatStepBlock) and block.index in run.flushed_steps:
            return
        lines = _chat_finalized_block_scrollback_lines(run, block)
        if lines:
            self.deps.write_lines(lines)
        run.mark_block_flushed(block)

    def flush_terminal_event(self, run: _ChatRun) -> None:
        lines = _chat_terminal_event_lines(run, _chat_completed_line_for)
        if lines:
            self.deps.write_lines(lines)

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


def _chat_active_step_line(step: _ChatStepBlock) -> str:
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
        if isinstance(run.mutable_block, _ChatStepBlock):
            run.mutable_block = None
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


def _chat_panel_user_block(run: _ChatRun) -> list[str]:
    spec = _chat_panel_user_bar_spec(run)
    return [] if spec is None else _chat_input_bar_plain_lines(spec)


def _chat_scrollback_user_block(run: _ChatRun) -> list[str]:
    spec = _chat_panel_user_bar_spec(run)
    return [] if spec is None else _chat_input_bar_ansi_lines(spec)


def _chat_panel_user_bar_spec(run: _ChatRun) -> _ChatInputBarSpec | None:
    if 0 in run.flushed_commands:
        return None
    block = _chat_existing_run_start_block(run)
    if block is None:
        return None
    return _chat_run_start_bar_spec(run, block=block)


def _chat_run_start_bar_spec(run: _ChatRun, *, block: _ChatRunStartBlock) -> _ChatInputBarSpec:
    text = _event_message_text(block.payload.get("message")) or _event_message_text(block.payload.get("input")) or run.message
    footer = _chat_run_input_footer(run)
    return _ChatInputBarSpec(
        kind="normal",
        marker=">",
        text=text,
        footer=footer,
        footer_dim=bool(footer),
    )


def _chat_existing_run_start_block(run: _ChatRun) -> _ChatRunStartBlock | None:
    block = run.commands.get(0)
    return block if isinstance(block, _ChatRunStartBlock) else None


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


def _chat_run_lines(run: _ChatRun, *, include_steps: bool) -> list[str]:
    lines = _chat_scrollback_user_block(run)
    if lines:
        lines.append("")
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
    del step_renderer
    state_line = _chat_run_state_line(run)
    queue_line = _chat_queue_activity_line(run)
    if queue_line or _chat_flow_stage_lines(run):
        return [_chat_line_fragments(line) for line in _chat_run_activity_lines(run, _chat_completed_line_for)]

    rows: list[list[tuple[str, str]]] = []
    if state_line and not _chat_state_line_after_activity(run):
        rows.append(_chat_line_fragments(state_line))
    for block in _chat_run_activity_blocks(run):
        rows.extend(_chat_block_activity_fragment_rows(run, block))
    if state_line and _chat_state_line_after_activity(run):
        rows.append(_chat_line_fragments(state_line))
    return rows


def _chat_line_fragments(line: str) -> list[tuple[str, str]]:
    return cast(list[tuple[str, str]], to_formatted_text(ANSI(line)))


def _chat_run_activity_lines(run: _ChatRun, step_renderer: Callable[[_ChatRun, int], str]) -> list[str]:
    del step_renderer
    state_line = _chat_run_state_line(run)
    queue_line = _chat_queue_activity_line(run)
    if queue_line:
        visible_queue_line = [] if _chat_existing_run_start_block(run) is not None else [queue_line]
        return [
            *visible_queue_line,
            *_chat_terminal_event_lines(run, _chat_completed_line_for),
            *_chat_run_result_lines(run),
        ]
    lines: list[str] = []
    if state_line and not _chat_state_line_after_activity(run):
        lines.append(state_line)
    for block in _chat_run_activity_blocks(run):
        lines.extend(_chat_block_activity_lines(run, block))
    if state_line and _chat_state_line_after_activity(run):
        lines.append(state_line)
    lines.extend(_chat_run_result_lines(run))
    return lines


def _chat_state_line_after_activity(run: _ChatRun) -> bool:
    status = _chat_run_display_status(run.status)
    return status in {"succeeded", "finished", "completed", "done", "failed", "error", "canceled", "cancelled"}


def _chat_run_activity_blocks(run: _ChatRun) -> list[_ChatMutableBlock]:
    if _chat_flow_stage_lines(run):
        return [
            _ChatFlowProjectionBlock(index=0, kind="flow_projection"),
            *_chat_unflushed_command_blocks(run),
        ]
    blocks: list[_ChatMutableBlock] = []
    rendered_steps: set[int] = set()
    timeline = run.timeline or [("step", index) for index in run.step_indexes()]
    for kind, index in timeline:
        if kind == "command":
            command = _chat_unflushed_command_block(run, index)
            if command is not None:
                blocks.append(command)
            continue
        if index in rendered_steps:
            continue
        if index in run.flushed_steps:
            rendered_steps.add(index)
            continue
        step = _chat_step_block_for_index(run, index)
        if step is not None:
            blocks.append(step)
            rendered_steps.add(index)
    for index in run.step_indexes():
        if index in rendered_steps or index in run.flushed_steps:
            continue
        step = _chat_step_block_for_index(run, index)
        if step is not None:
            blocks.append(step)
    active = run.mutable_block
    if (
        active is not None
        and active not in blocks
        and not isinstance(active, _ChatRunStartBlock)
        and not (isinstance(active, _ChatStepBlock) and active.index in run.flushed_steps)
        and not (isinstance(active, _ChatCommandBlock) and active.index in run.flushed_commands)
    ):
        blocks.append(active)
    return blocks


def _chat_unflushed_command_blocks(run: _ChatRun) -> list[_ChatCommandBlock]:
    blocks: list[_ChatCommandBlock] = []
    for kind, index in run.timeline:
        if kind != "command":
            continue
        command = _chat_unflushed_command_block(run, index)
        if command is not None:
            blocks.append(command)
    return blocks


def _chat_unflushed_command_block(run: _ChatRun, index: int) -> _ChatCommandBlock | None:
    command = run.commands.get(index)
    if command is None or isinstance(command, _ChatRunStartBlock) or index in run.flushed_commands:
        return None
    return command


def _chat_step_block_for_index(run: _ChatRun, index: int) -> _ChatStepBlock | None:
    active = run.steps.get(index)
    if active is not None:
        return active
    payload = run.completed_steps.get(index)
    if payload is None:
        return None
    block = _chat_step_block(payload, run=run)
    block.finalize(payload)
    return block


def _chat_block_activity_lines(run: _ChatRun, block: _ChatMutableBlock) -> list[str]:
    return block.render_activity_lines(run)


def _chat_finalized_block_scrollback_lines(run: _ChatRun, block: _ChatMutableBlock) -> list[str]:
    if isinstance(block, _ChatRunStartBlock):
        return [*_chat_scrollback_user_block(run), ""]
    if isinstance(block, _ChatRunEndBlock):
        return block.render_activity_lines(run)
    if isinstance(block, _ChatStepBlock) and block.kind in {"step", "parallel", "bind", "run"}:
        lines = _chat_flow_step_lines(run, block.payload)
        if lines:
            return lines
    lines = _chat_block_activity_lines(run, block)
    while lines and lines[-1] == "":
        lines.pop()
    if lines and isinstance(block, _ChatRunSteerBlock):
        lines.append("")
    return lines


def _chat_block_activity_fragment_rows(
    run: _ChatRun,
    block: _ChatMutableBlock,
) -> list[list[tuple[str, str]]]:
    return block.render_activity_fragment_rows(run)


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
    return _chat_flow_stage_lines_for(run, stages, calls)


def _chat_flow_step_lines(run: _ChatRun, payload: Mapping[str, Any]) -> list[str]:
    stages, calls = project_flow_from_step_payloads([payload])
    return _chat_flow_stage_lines_for(run, stages, calls)


def _chat_flow_stage_lines_for(
    run: _ChatRun,
    stages: Sequence[FlowStageView],
    calls: Mapping[str, FlowCallView],
) -> list[str]:
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


configure_chat_bottom_hooks(
    ChatBottomHooks(
        block_index=_chat_block_index,
        command_index=_chat_command_index,
        step_index=_chat_step_index,
        part_index=_chat_part_index,
        step_label=_chat_step_label,
        active_step_line=_chat_active_step_line,
        completed_step_line=lambda payload, run: _chat_completed_step_line(payload, run=run),
        line_fragments=_chat_line_fragments,
        run_is_stopped=_chat_run_is_stopped,
        run_result_lines=_chat_run_result_lines,
        flow_stage_lines=_chat_flow_stage_lines,
        steer_input_block=lambda command, waiting: _chat_steer_input_block(command, waiting=waiting),
        steer_input_fragment_rows=lambda command, waiting: _chat_steer_input_fragment_rows(command, waiting=waiting),
        terminal_event_payload=_chat_is_terminal_event_payload,
        message_lines=_chat_message_lines,
        marker_for=_chat_marker_for,
        model_tool_requests_have_results=_chat_model_tool_requests_have_results,
        friendly_error=_chat_friendly_error,
        stopped_run_message=_chat_stopped_run_message,
        record_system_event=lambda run, message, clear_active: _chat_record_system_event(
            run,
            message,
            clear_active=clear_active,
        ),
        panel_user_bar_spec=_chat_panel_user_bar_spec,
        panel_user_block=_chat_panel_user_block,
        active_activity_fragment_rows=_chat_active_activity_fragment_rows,
        run_activity_lines=_chat_run_activity_lines,
        summarize=_chat_summarize,
    )
)
