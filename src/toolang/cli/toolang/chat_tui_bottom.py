"""Mutable blocks and prompt-toolkit bottom widgets for the chat TUI."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension

from ..chat_history import ChatInputHistoryStore
from .chat_tui_commands import _chat_status_segments
from .chat_tui_input import _chat_input_bar_fragments, _chat_join_fragment_rows
from .chat_tui_theme import _CHAT_DIM, _CHAT_NORMAL_INTENSITY, _chat_dim, _chat_display_len, _chat_terminal_width
from .chat_tui_values import _display_run_status, _event_message_text, _event_parts_text, _int_or_none, _mapping, _text

_CHAT_MAX_INPUT_ROWS = 6
_CHAT_MAX_QUEUE_ROWS = 4


@dataclass(frozen=True, slots=True)
class ChatBottomHooks:
    block_index: Callable[[Mapping[str, Any]], int]
    command_index: Callable[[Mapping[str, Any]], int]
    step_index: Callable[[Mapping[str, Any]], int]
    part_index: Callable[[Mapping[str, Any]], int]
    step_label: Callable[[Mapping[str, Any], Any], str]
    active_step_line: Callable[[Any], str]
    completed_step_line: Callable[[Mapping[str, Any], Any], str]
    line_fragments: Callable[[str], list[tuple[str, str]]]
    run_is_stopped: Callable[[Any], bool]
    run_result_lines: Callable[[Any], list[str]]
    flow_stage_lines: Callable[[Any], list[str]]
    steer_input_block: Callable[[Mapping[str, Any], bool], list[str]]
    steer_input_fragment_rows: Callable[[Mapping[str, Any], bool], list[list[tuple[str, str]]]]
    terminal_event_payload: Callable[[Any, int, Mapping[str, Any]], bool]
    message_lines: Callable[[str, str], list[str]]
    marker_for: Callable[[str | None], str]
    model_tool_requests_have_results: Callable[[Any, int], bool]
    friendly_error: Callable[[str], str]
    stopped_run_message: Callable[[str, str | None], str]
    record_system_event: Callable[[Any, str, bool], None]
    panel_user_bar_spec: Callable[[Any], Any]
    panel_user_block: Callable[[Any], list[str]]
    active_activity_fragment_rows: Callable[[Any, Callable[[Any, int], str]], list[list[tuple[str, str]]]]
    run_activity_lines: Callable[[Any, Callable[[Any, int], str]], list[str]]
    summarize: Callable[[str], str]


_hooks: ChatBottomHooks | None = None


def configure_chat_bottom_hooks(hooks: ChatBottomHooks) -> None:
    global _hooks
    _hooks = hooks


def _chat_bottom_hooks() -> ChatBottomHooks:
    if _hooks is None:
        raise RuntimeError("chat bottom hooks are not configured")
    return _hooks


def _chat_block_index(payload: Mapping[str, Any]) -> int:
    return _chat_bottom_hooks().block_index(payload)


def _chat_command_index(payload: Mapping[str, Any]) -> int:
    return _chat_bottom_hooks().command_index(payload)


def _chat_step_index(payload: Mapping[str, Any]) -> int:
    return _chat_bottom_hooks().step_index(payload)


def _chat_part_index(payload: Mapping[str, Any]) -> int:
    return _chat_bottom_hooks().part_index(payload)


def _chat_step_label(payload: Mapping[str, Any], run: Any | None = None) -> str:
    return _chat_bottom_hooks().step_label(payload, run)


def _chat_active_step_line(step: Any) -> str:
    return _chat_bottom_hooks().active_step_line(step)


def _chat_completed_step_line(payload: Mapping[str, Any], *, run: Any | None = None) -> str:
    return _chat_bottom_hooks().completed_step_line(payload, run)


def _chat_line_fragments(line: str) -> list[tuple[str, str]]:
    return _chat_bottom_hooks().line_fragments(line)


def _chat_run_is_stopped(run: Any) -> bool:
    return _chat_bottom_hooks().run_is_stopped(run)


def _chat_run_result_lines(run: Any) -> list[str]:
    return _chat_bottom_hooks().run_result_lines(run)


def _chat_flow_stage_lines(run: Any) -> list[str]:
    return _chat_bottom_hooks().flow_stage_lines(run)


def _chat_steer_input_block(command: Mapping[str, Any], *, waiting: bool) -> list[str]:
    return _chat_bottom_hooks().steer_input_block(command, waiting)


def _chat_steer_input_fragment_rows(command: Mapping[str, Any], *, waiting: bool) -> list[list[tuple[str, str]]]:
    return _chat_bottom_hooks().steer_input_fragment_rows(command, waiting)


def _chat_is_terminal_event_payload(run: Any, index: int, payload: Mapping[str, Any]) -> bool:
    return _chat_bottom_hooks().terminal_event_payload(run, index, payload)


def _chat_message_lines(marker: str, text: str) -> list[str]:
    return _chat_bottom_hooks().message_lines(marker, text)


def _chat_marker_for(kind: str | None) -> str:
    return _chat_bottom_hooks().marker_for(kind)


def _chat_model_tool_requests_have_results(run: Any, model_step_index: int) -> bool:
    return _chat_bottom_hooks().model_tool_requests_have_results(run, model_step_index)


def _chat_friendly_error(message: str) -> str:
    return _chat_bottom_hooks().friendly_error(message)


def _chat_stopped_run_message(status: str, error: str | None) -> str:
    return _chat_bottom_hooks().stopped_run_message(status, error)


def _chat_record_system_event(run: Any, message: str, *, clear_active: bool) -> None:
    _chat_bottom_hooks().record_system_event(run, message, clear_active)


def _chat_panel_user_bar_spec(run: Any) -> Any:
    return _chat_bottom_hooks().panel_user_bar_spec(run)


def _chat_panel_user_block(run: Any) -> list[str]:
    return _chat_bottom_hooks().panel_user_block(run)


def _chat_active_activity_fragment_rows(run: Any, step_renderer: Callable[[Any, int], str]) -> list[list[tuple[str, str]]]:
    return _chat_bottom_hooks().active_activity_fragment_rows(run, step_renderer)


def _chat_run_activity_lines(run: Any, step_renderer: Callable[[Any, int], str]) -> list[str]:
    return _chat_bottom_hooks().run_activity_lines(run, step_renderer)


def _chat_summarize(message: str) -> str:
    return _chat_bottom_hooks().summarize(message)


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

    def render_activity_lines(self, run: "_ChatRun") -> list[str]:
        del run
        return []

    def render_activity_fragment_rows(self, run: "_ChatRun") -> list[list[tuple[str, str]]]:
        return [_chat_line_fragments(line) for line in self.render_activity_lines(run)]


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
class _ChatRunStartBlock(_ChatCommandBlock):
    """Start-command block: create on run_starting, finalize on run_begin."""


@dataclass(slots=True)
class _ChatRunSteerBlock(_ChatCommandBlock):
    """Steer-command block: create on run_steering, finalize on next step_begin."""

    def render_activity_lines(self, run: "_ChatRun") -> list[str]:
        del run
        return _chat_steer_input_block(self.payload, waiting=not self.finalized)

    def render_activity_fragment_rows(self, run: "_ChatRun") -> list[list[tuple[str, str]]]:
        del run
        return _chat_steer_input_fragment_rows(self.payload, waiting=not self.finalized)


@dataclass(slots=True)
class _ChatRunStopBlock(_ChatCommandBlock):
    """Stop-command block: create on run_stopping, finalize on run_end."""

    def render_activity_lines(self, run: "_ChatRun") -> list[str]:
        return [] if self.finalized or _chat_run_is_stopped(run) else [_chat_dim("canceling...")]

    def render_activity_fragment_rows(self, run: "_ChatRun") -> list[list[tuple[str, str]]]:
        return [] if self.finalized or _chat_run_is_stopped(run) else [_chat_line_fragments(_chat_dim("canceling..."))]


@dataclass(slots=True)
class _ChatRunEndBlock(_ChatMutableBlock):
    """Run-end block: create and finalize on run_end."""

    @classmethod
    def create(cls, payload: Mapping[str, Any], *, run: "_ChatRun | None" = None) -> "_ChatRunEndBlock":
        del run
        return cls(index=0, kind="run_end", payload=dict(payload))

    def render_activity_lines(self, run: "_ChatRun") -> list[str]:
        return [*_chat_run_result_lines(run), ""]


@dataclass(slots=True)
class _ChatFlowProjectionBlock(_ChatMutableBlock):
    """Flow projection block: renders flow stage state from the run's step blocks."""

    def render_activity_lines(self, run: "_ChatRun") -> list[str]:
        return _chat_flow_stage_lines(run)


def _chat_command_block(payload: Mapping[str, Any]) -> _ChatCommandBlock:
    kind = str(payload.get("kind") or "command")
    block_cls: type[_ChatCommandBlock]
    if kind == "start":
        block_cls = _ChatRunStartBlock
    elif kind == "steer":
        block_cls = _ChatRunSteerBlock
    elif kind == "stop":
        block_cls = _ChatRunStopBlock
    else:
        block_cls = _ChatCommandBlock
    return block_cls.create(payload)


@dataclass(slots=True)
class _ChatStepBlock(_ChatMutableBlock):
    label: str = ""
    frame: int = 0
    part_deltas: dict[int, list[str]] = field(default_factory=dict)

    @classmethod
    def create(cls, payload: Mapping[str, Any], *, run: "_ChatRun | None" = None) -> "_ChatStepBlock":
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

    def render_activity_lines(self, run: "_ChatRun") -> list[str]:
        if not self.finalized:
            return [_chat_active_step_line(self)]
        payload = self.payload
        if _chat_is_terminal_event_payload(run, self.index, payload):
            return []
        return [_chat_completed_step_line(payload, run=run)]


@dataclass(slots=True)
class _ChatModelStepBlock(_ChatStepBlock):
    """Model step block: create on step_begin, update on part_delta, finalize on step_end."""

    def render_activity_lines(self, run: "_ChatRun") -> list[str]:
        if not self.finalized:
            return [_chat_active_step_line(self)]
        payload = self.payload
        if _chat_is_terminal_event_payload(run, self.index, payload):
            return []
        text = _event_parts_text(payload.get("output"))
        if text:
            return _chat_message_lines(_chat_marker_for("model"), text)
        if _chat_model_tool_requests_have_results(run, self.index):
            return []
        return [_chat_completed_step_line(payload, run=run)]


@dataclass(slots=True)
class _ChatToolStepBlock(_ChatStepBlock):
    """Tool step block: create on step_begin, update on part_delta, finalize on step_end."""


def _chat_step_block(payload: Mapping[str, Any], *, run: "_ChatRun | None") -> _ChatStepBlock:
    kind = str(payload.get("kind") or "")
    block_cls: type[_ChatStepBlock]
    if kind == "model":
        block_cls = _ChatModelStepBlock
    elif kind == "tool":
        block_cls = _ChatToolStepBlock
    else:
        block_cls = _ChatStepBlock
    return block_cls.create(payload, run=run)


def _chat_run_end_block(payload: Mapping[str, Any], *, run: "_ChatRun") -> _ChatRunEndBlock:
    return run.complete_run(payload)


@dataclass(frozen=True, slots=True)
class _ChatToolCall:
    name: str
    input: dict[str, Any]


@dataclass(slots=True)
class _ChatQueueItem:
    kind: Literal["run", "steer"]
    text: str


@dataclass(frozen=True, slots=True)
class _ChatScrollbackAction:
    kind: Literal["queue_state", "block", "terminal_event"]
    block: _ChatMutableBlock | None = None


@dataclass(slots=True)
class _ChatTraceEventResult:
    scrollback: list[_ChatScrollbackAction] = field(default_factory=list)
    send_cancel_request: bool = False
    run_finished: bool = False

    def flush_block(self, block: _ChatMutableBlock | None) -> None:
        if block is not None:
            self.scrollback.append(_ChatScrollbackAction("block", block))

    def flush_blocks(self, blocks: Sequence[_ChatMutableBlock]) -> None:
        for block in blocks:
            self.flush_block(block)

    def flush_queue_state(self) -> None:
        self.scrollback.append(_ChatScrollbackAction("queue_state"))

    def flush_terminal_event(self) -> None:
        self.scrollback.append(_ChatScrollbackAction("terminal_event"))


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
    steps: dict[int, _ChatStepBlock] = field(default_factory=dict)
    completed_steps: dict[int, dict[str, Any]] = field(default_factory=dict)
    tool_calls_by_part: dict[tuple[int, int], _ChatToolCall] = field(default_factory=dict)
    commands: dict[int, _ChatCommandBlock] = field(default_factory=dict)
    timeline: list[tuple[Literal["step", "command"], int]] = field(default_factory=list)
    mutable_block: _ChatMutableBlock | None = None
    flushed_steps: set[int] = field(default_factory=set)
    flushed_commands: set[int] = field(default_factory=set)
    child_runs: dict[str, _ChatRun] = field(default_factory=dict)

    def apply_trace_event(self, event_type: str, payload: Mapping[str, Any]) -> _ChatTraceEventResult:
        result = _ChatTraceEventResult()
        if event_type == "run_waiting":
            self.update_queue(event_type, payload)
        elif event_type == "run_starting":
            self.start_command(payload)
        elif event_type == "run_steering":
            command = dict(payload)
            command["kind"] = "steer"
            self.record_command(command)
        elif event_type == "run_stopping":
            command = dict(payload)
            command["kind"] = "stop"
            self.record_command(command)
            self.request_cancel()
        elif event_type == "run_begin":
            self.mark_running()
            result.flush_block(self.finalize_command(0, payload))
            result.send_cancel_request = True
        elif event_type == "step_begin":
            result.flush_blocks(self.start_step(dict(payload)))
        elif event_type == "part_begin":
            self.update_step(payload)
        elif event_type == "part_delta":
            self.delta_step(payload)
        elif event_type == "part_end":
            self.record_part(dict(payload))
        elif event_type == "step_end":
            result.flush_block(self.complete_step(dict(payload)))
        elif event_type == "run_end":
            return self.finish_trace(payload)
        return result

    def finish_trace(self, payload: Mapping[str, Any]) -> _ChatTraceEventResult:
        result = _ChatTraceEventResult(run_finished=True)
        result.flush_queue_state()
        start_block = self.finalize_command(0, payload)
        if start_block is not None and start_block.index not in self.flushed_commands:
            result.flush_block(start_block)
        result.flush_blocks(self.finalize_pending_steer_blocks())
        result.flush_blocks(self.finalize_stop_blocks(payload))
        self.status = _display_run_status(payload.get("status")) or "completed"
        error = _text(payload.get("error"))
        if error:
            self.terminal_error = _chat_friendly_error(error)
        if self.status in {"failed", "error", "canceled", "cancelled"}:
            message = _chat_stopped_run_message(
                self.status,
                self.terminal_error if error else None,
            )
            if error:
                message = f"error: {message}"
            _chat_record_system_event(self, message, clear_active=True)
            result.flush_terminal_event()
        result.flush_block(self.complete_run(payload))
        return result

    def start_step(self, payload: dict[str, Any]) -> list[_ChatRunSteerBlock]:
        step = _chat_step_block(payload, run=self)
        index = step.index
        finalized = self.finalize_pending_steer_blocks()
        self.remember_timeline("step", index)
        self.steps[index] = step
        self.mutable_block = step
        return finalized

    def update_step(self, payload: Mapping[str, Any]) -> None:
        step = self.steps.get(_chat_step_index(payload))
        if step is not None:
            step.update(payload)

    def delta_step(self, payload: Mapping[str, Any]) -> None:
        step = self.steps.get(_chat_step_index(payload))
        if step is not None:
            step.delta(payload)

    def complete_step(self, payload: dict[str, Any]) -> _ChatStepBlock:
        index = _chat_step_index(payload)
        self.remember_timeline("step", index)
        active_step = self.steps.get(index)
        if active_step is None:
            active_step = _chat_step_block(payload, run=self)
        completed_payload = active_step.finalize(payload)
        self.completed_steps[index] = completed_payload
        self.steps.pop(index, None)
        if self.mutable_block is active_step:
            self.mutable_block = None
        return active_step

    def complete_run(self, payload: Mapping[str, Any]) -> _ChatRunEndBlock:
        block = _ChatRunEndBlock.create(payload, run=self)
        self.mutable_block = block
        block.finalize(payload)
        if self.mutable_block is block:
            self.mutable_block = None
        return block

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
        block = _chat_command_block(payload)
        index = block.index
        if request_id := _text(payload.get("request_id")):
            existing_index = self.command_index_for_request(request_id)
            if existing_index is not None and existing_index != index:
                existing = self.commands.pop(existing_index, None)
                if self.mutable_block is existing:
                    self.mutable_block = None
                self.flushed_commands.discard(existing_index)
                self.timeline = [
                    item
                    for item in self.timeline
                    if item != ("command", existing_index)
                ]
        self.remember_timeline("command", index)
        self.commands[index] = block
        if isinstance(block, (_ChatRunSteerBlock, _ChatRunStopBlock)):
            self.mutable_block = block
            return
        block.finalize(payload)

    def command_index_for_request(self, request_id: str) -> int | None:
        for index, block in self.commands.items():
            if _text(block.payload.get("request_id")) == request_id:
                return index
        return None

    def finalize_pending_steer_blocks(self) -> list[_ChatRunSteerBlock]:
        finalized: list[_ChatRunSteerBlock] = []
        for block in self.commands.values():
            if isinstance(block, _ChatRunSteerBlock) and not block.finalized:
                block.finalize({})
                if self.mutable_block is block:
                    self.mutable_block = None
                finalized.append(block)
        return finalized

    def finalize_stop_blocks(self, payload: Mapping[str, Any]) -> list[_ChatRunStopBlock]:
        finalized: list[_ChatRunStopBlock] = []
        for block in self.commands.values():
            if isinstance(block, _ChatRunStopBlock) and not block.finalized:
                block.finalize(payload)
                if self.mutable_block is block:
                    self.mutable_block = None
                finalized.append(block)
        return finalized

    def next_command_index(self) -> int:
        return max(self.commands, default=0) + 1

    def start_command(self, payload: Mapping[str, Any]) -> None:
        command_payload = dict(payload)
        command_payload.setdefault("kind", "start")
        command_payload.setdefault("ref", {"kind": "command", "index": 0})
        if "message" not in command_payload and "input" in command_payload:
            command_payload["message"] = command_payload["input"]
        block = _chat_command_block(command_payload)
        self.remember_timeline("command", block.index)
        self.commands[block.index] = block
        self.mutable_block = block

    def finalize_command(self, index: int, payload: Mapping[str, Any]) -> _ChatCommandBlock | None:
        block = self.commands.get(index)
        if block is None:
            return None
        block.finalize(payload)
        if self.mutable_block is block:
            self.mutable_block = None
        return block

    def mark_block_flushed(self, block: _ChatMutableBlock) -> None:
        if isinstance(block, _ChatStepBlock):
            self.flushed_steps.add(block.index)
        elif isinstance(block, _ChatCommandBlock):
            self.flushed_commands.add(block.index)

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
                    ConditionalContainer(
                        Window(
                            self.user_view,
                            height=self.user_rows,
                            wrap_lines=False,
                            always_hide_cursor=True,
                            style="class:normal-input",
                            char=" ",
                        ),
                        filter=Condition(lambda: bool(self.user_lines())),
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
        spec = _chat_panel_user_bar_spec(run)
        return [] if spec is None else _chat_input_bar_fragments(spec)

    def render_activity(self) -> list[tuple[str, str]]:
        run = self.get_run()
        if run is None:
            return []
        rows = _chat_active_activity_fragment_rows(run, self.step_line)
        if self.user_lines():
            rows = [*rows, []]
        else:
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
        if self.user_lines():
            return [*lines, ""]
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
