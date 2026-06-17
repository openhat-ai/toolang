"""Compact core sketch for the chat TUI.

This file is intentionally not wired into the production CLI yet.  It shows the
shape of a smaller one-file implementation: prompt events mutate active/queued
runs, trace events drive mutable blocks, finalized blocks move to scrollback,
and the UI can repaint from the current active run at any time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast


PromptCommand = Literal["start", "steer", "stop"]
TimelineKind = Literal["command", "step"]
ScrollbackKind = Literal["block", "line"]


@dataclass(frozen=True, slots=True)
class PromptRequest:
    command: PromptCommand
    text: str = ""
    run_id: str = ""


@dataclass(frozen=True, slots=True)
class ScrollbackAction:
    kind: ScrollbackKind
    lines: tuple[str, ...] = ()
    block: "MutableBlock | None" = None


@dataclass(slots=True)
class TraceResult:
    scrollback: list[ScrollbackAction] = field(default_factory=list)
    send_cancel: bool = False
    run_finished: bool = False

    def flush_block(self, block: "MutableBlock | None") -> None:
        if block is not None:
            self.scrollback.append(ScrollbackAction("block", block=block))

    def flush_blocks(self, blocks: Sequence["MutableBlock"]) -> None:
        for block in blocks:
            self.flush_block(block)

    def flush_lines(self, lines: Sequence[str]) -> None:
        if lines:
            self.scrollback.append(ScrollbackAction("line", lines=tuple(lines)))


@dataclass(slots=True)
class MutableBlock:
    index: int
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    finalized: bool = False

    @classmethod
    def create(cls, payload: Mapping[str, Any], *, run: "Run | None" = None) -> "MutableBlock":
        del run
        return cls(index=block_index(payload), kind=text(payload.get("kind")) or "unknown", payload=dict(payload))

    def update(self, payload: Mapping[str, Any]) -> None:
        self.payload.update(payload)

    def delta(self, payload: Mapping[str, Any]) -> None:
        self.update(payload)

    def finalize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.update(payload)
        self.finalized = True
        return dict(self.payload)

    def lines(self, run: "Run") -> list[str]:
        del run
        return []


@dataclass(slots=True)
class CommandBlock(MutableBlock):
    @classmethod
    def create(cls, payload: Mapping[str, Any], *, run: "Run | None" = None) -> "CommandBlock":
        del run
        return cls(index=command_index(payload), kind=text(payload.get("kind")) or "command", payload=dict(payload))


@dataclass(slots=True)
class RunStartBlock(CommandBlock):
    def lines(self, run: "Run") -> list[str]:
        message = event_message_text(self.payload.get("message")) or event_message_text(self.payload.get("input")) or run.message
        footer = queue_line(run) or run.run_id
        return input_bar(">", message, footer=footer)


@dataclass(slots=True)
class RunSteerBlock(CommandBlock):
    def lines(self, run: "Run") -> list[str]:
        del run
        footer = "" if self.finalized else "pending for next step"
        return input_bar("+", event_message_text(self.payload.get("message")), footer=footer, outer_blank=True)


@dataclass(slots=True)
class RunStopBlock(CommandBlock):
    def lines(self, run: "Run") -> list[str]:
        if self.finalized or run.status in {"canceled", "cancelled", "failed", "finished", "succeeded"}:
            return []
        return ["canceling..."]


@dataclass(slots=True)
class RunEndBlock(MutableBlock):
    @classmethod
    def create(cls, payload: Mapping[str, Any], *, run: "Run | None" = None) -> "RunEndBlock":
        del run
        return cls(index=0, kind="run_end", payload=dict(payload))

    def lines(self, run: "Run") -> list[str]:
        return [*run_result_lines(run), ""]


@dataclass(slots=True)
class StepBlock(MutableBlock):
    label: str = ""
    part_deltas: dict[int, list[str]] = field(default_factory=dict)

    @classmethod
    def create(cls, payload: Mapping[str, Any], *, run: "Run | None" = None) -> "StepBlock":
        kind = text(payload.get("kind")) or "unknown"
        return cls(index=step_index(payload), kind=kind, payload=dict(payload), label=step_label(payload, run))

    def delta(self, payload: Mapping[str, Any]) -> None:
        super().delta(payload)
        delta = mapping(payload.get("delta"))
        if delta.get("type") == "text" and text(delta.get("text")):
            self.part_deltas.setdefault(part_index(payload), []).append(text(delta.get("text")) or "")

    def text_delta(self) -> str:
        return "".join(chunk for index in sorted(self.part_deltas) for chunk in self.part_deltas[index])

    def finalize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        completed = dict(payload)
        if "input" not in completed and "input" in self.payload:
            completed["input"] = self.payload["input"]
        self.update(completed)
        self.finalized = True
        return completed

    def lines(self, run: "Run") -> list[str]:
        if not self.finalized:
            return [active_step_line(self)]
        if self.kind in {"step", "parallel", "bind", "run"}:
            return flow_step_lines(self.payload)
        return [completed_step_line(self.payload, run)]


@dataclass(slots=True)
class ModelStepBlock(StepBlock):
    def lines(self, run: "Run") -> list[str]:
        if not self.finalized:
            preview = " ".join(self.text_delta().split())
            return [f"* {preview}" if preview else "* thinking..."]
        message = event_parts_text(self.payload.get("output"))
        if message:
            return message_lines("*", message)
        requests = model_tool_request_summary(self.payload)
        if requests and model_tool_requests_have_results(run, self.index):
            return []
        if requests:
            return [f"* requested {requests}"]
        return ["* [no text message]"]


@dataclass(slots=True)
class ToolStepBlock(StepBlock):
    pass


@dataclass(slots=True)
class FlowProjectionBlock(MutableBlock):
    def lines(self, run: "Run") -> list[str]:
        return flow_projection_lines(run)


@dataclass(slots=True)
class ToolCall:
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Run:
    run_id: str
    message: str
    status: str = "submitting"
    queue_reason: str = ""
    queue_position: int | None = None
    terminal_error: str = ""
    cancel_requested: bool = False
    started: bool = False
    steps: dict[int, StepBlock] = field(default_factory=dict)
    completed_steps: dict[int, dict[str, Any]] = field(default_factory=dict)
    commands: dict[int, CommandBlock] = field(default_factory=dict)
    timeline: list[tuple[TimelineKind, int]] = field(default_factory=list)
    tool_calls: dict[tuple[int, int], ToolCall] = field(default_factory=dict)
    flushed_steps: set[int] = field(default_factory=set)
    flushed_commands: set[int] = field(default_factory=set)
    mutable_block: MutableBlock | None = None
    child_runs: dict[str, "Run"] = field(default_factory=dict)

    def apply(self, event_type: str, payload: Mapping[str, Any]) -> TraceResult:
        result = TraceResult()
        if event_type == "run_waiting":
            self.update_queue(payload)
        elif event_type == "run_starting":
            self.start_command(payload)
        elif event_type == "run_steering":
            self.record_command({**payload, "kind": "steer"})
        elif event_type == "run_stopping":
            self.record_command({**payload, "kind": "stop"})
            self.request_cancel()
        elif event_type == "run_begin":
            self.mark_running()
            result.flush_block(self.finalize_command(0, payload))
            result.send_cancel = self.cancel_requested
        elif event_type == "step_begin":
            result.flush_blocks(self.start_step(payload))
        elif event_type == "part_begin":
            self.update_step(payload)
        elif event_type == "part_delta":
            self.delta_step(payload)
        elif event_type == "part_end":
            self.record_part(payload)
        elif event_type == "step_end":
            result.flush_block(self.complete_step(payload))
        elif event_type == "run_end":
            return self.finish(payload)
        return result

    def finish(self, payload: Mapping[str, Any]) -> TraceResult:
        result = TraceResult(run_finished=True)
        result.flush_lines([queue_line(self)])
        start = self.finalize_command(0, payload)
        if start is not None and start.index not in self.flushed_commands:
            result.flush_block(start)
        result.flush_blocks(self.finalize_pending_steers())
        result.flush_blocks(self.finalize_stops(payload))
        self.status = display_status(payload.get("status")) or "completed"
        self.terminal_error = text(payload.get("error")) or ""
        if self.status in {"failed", "error", "canceled", "cancelled"}:
            result.flush_lines(run_result_lines(self))
        result.flush_block(self.complete_run(payload))
        return result

    def start_command(self, payload: Mapping[str, Any]) -> None:
        command = dict(payload)
        command.setdefault("kind", "start")
        command.setdefault("ref", {"kind": "command", "index": 0})
        command.setdefault("message", command.get("input"))
        block = command_block(command)
        self.remember("command", block.index)
        self.commands[block.index] = block
        self.mutable_block = block

    def record_command(self, payload: Mapping[str, Any]) -> None:
        block = command_block(payload)
        self.remember("command", block.index)
        self.commands[block.index] = block
        if isinstance(block, RunSteerBlock | RunStopBlock):
            self.mutable_block = block
        else:
            block.finalize(payload)

    def finalize_command(self, index: int, payload: Mapping[str, Any]) -> CommandBlock | None:
        block = self.commands.get(index)
        if block is None:
            return None
        block.finalize(payload)
        if self.mutable_block is block:
            self.mutable_block = None
        return block

    def start_step(self, payload: Mapping[str, Any]) -> list[MutableBlock]:
        finalized = self.finalize_pending_steers()
        block = step_block(payload, self)
        self.remember("step", block.index)
        self.steps[block.index] = block
        self.mutable_block = block
        return finalized

    def update_step(self, payload: Mapping[str, Any]) -> None:
        if block := self.steps.get(step_index(payload)):
            block.update(payload)

    def delta_step(self, payload: Mapping[str, Any]) -> None:
        if block := self.steps.get(step_index(payload)):
            block.delta(payload)

    def complete_step(self, payload: Mapping[str, Any]) -> StepBlock:
        index = step_index(payload)
        block = self.steps.get(index) or step_block(payload, self)
        self.remember("step", index)
        self.completed_steps[index] = block.finalize(payload)
        self.steps.pop(index, None)
        if self.mutable_block is block:
            self.mutable_block = None
        return block

    def complete_run(self, payload: Mapping[str, Any]) -> RunEndBlock:
        block = RunEndBlock.create(payload, run=self)
        self.mutable_block = block
        block.finalize(payload)
        self.mutable_block = None
        return block

    def finalize_pending_steers(self) -> list[MutableBlock]:
        return self.finalize_commands_of_type(RunSteerBlock, {})

    def finalize_stops(self, payload: Mapping[str, Any]) -> list[MutableBlock]:
        return self.finalize_commands_of_type(RunStopBlock, payload)

    def finalize_commands_of_type(
        self,
        block_type: type[RunSteerBlock] | type[RunStopBlock],
        payload: Mapping[str, Any],
    ) -> list[MutableBlock]:
        finalized: list[MutableBlock] = []
        for block in self.commands.values():
            if isinstance(block, block_type) and not block.finalized:
                block.finalize(payload)
                if self.mutable_block is block:
                    self.mutable_block = None
                finalized.append(block)
        return finalized

    def record_part(self, payload: Mapping[str, Any]) -> None:
        part = mapping(payload.get("part"))
        if part.get("type") != "tool_call":
            return
        name = text(part.get("tool_name")) or text(part.get("tool_family"))
        if name:
            self.tool_calls[(step_index(payload), part_index(payload))] = ToolCall(name, dict(mapping(part.get("input"))))

    def update_queue(self, payload: Mapping[str, Any]) -> None:
        self.run_id = text(payload.get("run_id")) or self.run_id
        self.status = "canceling" if self.cancel_requested else "waiting"
        self.queue_reason = text(payload.get("reason")) or "queue"
        self.queue_position = int_or_none(payload.get("position"))

    def mark_running(self) -> None:
        self.started = True
        self.status = "canceling" if self.cancel_requested else "running"
        self.queue_reason = ""
        self.queue_position = None

    def request_cancel(self) -> None:
        self.cancel_requested = True
        self.status = "canceling"
        self.queue_reason = ""
        self.queue_position = None

    def remember(self, kind: TimelineKind, index: int) -> None:
        item = (kind, index)
        if item not in self.timeline:
            self.timeline.append(item)

    def mark_flushed(self, block: MutableBlock) -> None:
        if isinstance(block, StepBlock):
            self.flushed_steps.add(block.index)
        elif isinstance(block, CommandBlock):
            self.flushed_commands.add(block.index)


@dataclass(slots=True)
class ChatCore:
    active_run: Run | None = None
    queued_runs: list[PromptRequest] = field(default_factory=list)
    scrollback: list[str] = field(default_factory=list)

    def on_prompt_submit(self, message: str) -> PromptRequest:
        request = PromptRequest("start", message)
        if self.active_run is None:
            return request
        self.queued_runs.append(request)
        return request

    def on_prompt_steer(self, message: str) -> PromptRequest | None:
        if self.active_run is None:
            return None
        return PromptRequest("steer", message, run_id=self.active_run.run_id)

    def on_prompt_stop(self) -> PromptRequest | None:
        if self.active_run is None:
            return None
        self.active_run.request_cancel()
        return PromptRequest("stop", run_id=self.active_run.run_id)

    def on_trace_event(self, event: Mapping[str, Any]) -> None:
        event_type = text(event.get("type")) or text(event.get("event_type")) or ""
        payload = mapping(event.get("payload"))
        run = self.run_for_event(event_type, payload)
        if run is None:
            return
        result = run.apply(event_type, payload)
        self.apply_result(run, result)

    def run_for_event(self, event_type: str, payload: Mapping[str, Any]) -> Run | None:
        if event_type == "run_starting":
            return self.ensure_starting_run(payload)
        if event_type == "run_waiting":
            if self.active_run is None:
                self.active_run = Run(text(payload.get("run_id")) or "", "", "waiting")
            return self.active_run
        if event_type == "run_begin":
            return self.ensure_running_run(payload)
        return self.active_run

    def ensure_starting_run(self, payload: Mapping[str, Any]) -> Run:
        run_id = text(payload.get("run_id")) or ""
        message = event_message_text(payload.get("input"))
        if self.active_run and (not self.active_run.run_id or self.active_run.run_id == run_id):
            self.active_run.run_id = run_id
            self.active_run.message = message or self.active_run.message
            return self.active_run
        self.active_run = Run(run_id, message, "submitting")
        return self.active_run

    def ensure_running_run(self, payload: Mapping[str, Any]) -> Run:
        run_id = text(payload.get("run_id")) or ""
        if self.active_run and (not self.active_run.run_id or self.active_run.run_id == run_id):
            self.active_run.run_id = run_id
            return self.active_run
        self.active_run = Run(run_id, event_message_text(payload.get("input")), "running")
        return self.active_run

    def apply_result(self, run: Run, result: TraceResult) -> None:
        for action in result.scrollback:
            if action.kind == "line":
                self.scrollback.extend(line for line in action.lines if line)
            elif action.kind == "block" and action.block is not None:
                self.flush_block(run, action.block)
        if result.run_finished:
            self.active_run = None
            if self.queued_runs:
                self.queued_runs.pop(0)

    def flush_block(self, run: Run, block: MutableBlock) -> None:
        if isinstance(block, CommandBlock) and block.index in run.flushed_commands:
            return
        if isinstance(block, StepBlock) and block.index in run.flushed_steps:
            return
        self.scrollback.extend(finalized_block_lines(run, block))
        run.mark_flushed(block)

    def active_lines(self) -> list[str]:
        return [] if self.active_run is None else active_run_lines(self.active_run)


def command_block(payload: Mapping[str, Any]) -> CommandBlock:
    kind = text(payload.get("kind")) or "command"
    if kind == "start":
        return RunStartBlock.create(payload)
    if kind == "steer":
        return RunSteerBlock.create(payload)
    if kind == "stop":
        return RunStopBlock.create(payload)
    return CommandBlock.create(payload)


def step_block(payload: Mapping[str, Any], run: Run | None) -> StepBlock:
    kind = text(payload.get("kind")) or ""
    if kind == "model":
        return ModelStepBlock.create(payload, run=run)
    if kind == "tool":
        return ToolStepBlock.create(payload, run=run)
    return StepBlock.create(payload, run=run)


def active_run_lines(run: Run) -> list[str]:
    lines: list[str] = []
    start = run.commands.get(0)
    if isinstance(start, RunStartBlock) and 0 not in run.flushed_commands:
        lines.extend(start.lines(run))
    body = activity_lines(run)
    if lines and body:
        lines.extend(body)
    elif body:
        lines.extend(["", *body, ""])
    return lines


def activity_lines(run: Run) -> list[str]:
    if queue_line(run):
        return [] if isinstance(run.commands.get(0), RunStartBlock) else [queue_line(run)]
    lines: list[str] = []
    for block in activity_blocks(run):
        lines.extend(block.lines(run))
    lines.extend(run_result_lines(run))
    return lines


def activity_blocks(run: Run) -> list[MutableBlock]:
    if flow_projection_lines(run):
        return [FlowProjectionBlock(0, "flow_projection"), *unflushed_commands(run)]
    blocks: list[MutableBlock] = []
    seen_steps: set[int] = set()
    for kind, index in run.timeline or [("step", index) for index in sorted(run.steps | run.completed_steps)]:
        if kind == "command":
            if command := unflushed_command(run, index):
                blocks.append(command)
            continue
        if index in seen_steps or index in run.flushed_steps:
            continue
        if block := step_block_for_index(run, index):
            blocks.append(block)
            seen_steps.add(index)
    if run.mutable_block and run.mutable_block not in blocks:
        blocks.append(run.mutable_block)
    return blocks


def unflushed_commands(run: Run) -> list[CommandBlock]:
    return [block for kind, index in run.timeline if kind == "command" if (block := unflushed_command(run, index))]


def unflushed_command(run: Run, index: int) -> CommandBlock | None:
    block = run.commands.get(index)
    if block is None or isinstance(block, RunStartBlock) or index in run.flushed_commands:
        return None
    return block


def step_block_for_index(run: Run, index: int) -> StepBlock | None:
    if active := run.steps.get(index):
        return active
    payload = run.completed_steps.get(index)
    if payload is None:
        return None
    block = step_block(payload, run)
    block.finalize(payload)
    return block


def finalized_block_lines(run: Run, block: MutableBlock) -> list[str]:
    if isinstance(block, RunStartBlock):
        return [*block.lines(run), ""]
    if isinstance(block, RunEndBlock):
        return block.lines(run)
    lines = block.lines(run)
    while lines and lines[-1] == "":
        lines.pop()
    if lines and isinstance(block, RunSteerBlock):
        lines.append("")
    return lines


def input_bar(marker: str, text_value: str, *, footer: str = "", outer_blank: bool = False) -> list[str]:
    rows = [""] if outer_blank else []
    rows.append("")
    for index, line in enumerate(text_value.splitlines() or [""]):
        rows.append(f"{marker} {line}" if index == 0 else f"  {line}")
    rows.append(f"  {footer}" if footer else "")
    if outer_blank:
        rows.append("")
    return rows


def active_step_line(block: StepBlock) -> str:
    if block.kind == "model":
        preview = " ".join(block.text_delta().split())
        return f"* {preview}" if preview else "* thinking..."
    return f"> running {block.label}"


def completed_step_line(payload: Mapping[str, Any], run: Run | None) -> str:
    kind = text(payload.get("kind")) or "step"
    if kind == "tool":
        return f"> ran {tool_call_display(tool_call(payload, run))}"
    if kind == "model":
        requests = model_tool_request_summary(payload)
        return f"* requested {requests}" if requests else "* [no text message]"
    if kind in {"step", "parallel", "bind", "run"}:
        return "ran " + (text(mapping(payload.get("payload")).get("op")) or kind)
    return f"- {kind} completed"


def message_lines(marker: str, value: str) -> list[str]:
    lines = value.splitlines() or [""]
    return [f"{marker} {lines[0]}", *(f"  {line}" for line in lines[1:])]


def run_result_lines(run: Run) -> list[str]:
    if run.status not in {"failed", "error", "canceled", "cancelled"}:
        return []
    title = f"-------- {run.run_id} {run.status} --------"
    return [f"  {title}", *(f"  {line}" for line in run.terminal_error.splitlines() if line)]


def queue_line(run: Run) -> str:
    if run.status != "waiting":
        return ""
    suffix = f" for {run.queue_reason}" if run.queue_reason else ""
    position = f" position {run.queue_position}" if run.queue_position is not None else ""
    return f"waiting {run.run_id}{suffix}{position}"


def step_label(payload: Mapping[str, Any], run: Run | None) -> str:
    kind = text(payload.get("kind")) or ""
    if kind == "model":
        return "thinking..."
    if kind == "tool":
        return tool_call_display(tool_call(payload, run))
    data = mapping(payload.get("payload")) or mapping(payload.get("metadata"))
    return text(data.get("op")) or kind or "step"


def tool_call(payload: Mapping[str, Any], run: Run | None) -> ToolCall:
    input_refs = list_value(payload.get("input"))
    for item in input_refs:
        item_map = mapping(item)
        if run is None:
            continue
        ref = (int_or_none(item_map.get("step_index")) or 0, int_or_none(item_map.get("part_index")) or 0)
        if ref in run.tool_calls:
            return run.tool_calls[ref]
    name = text(payload.get("tool_name")) or "tool"
    return ToolCall(name, dict(mapping(payload.get("input"))))


def tool_call_display(call: ToolCall) -> str:
    summary = tool_input_summary(call.input)
    return f"{call.name}: {summary}" if summary else call.name


def model_tool_request_summary(payload: Mapping[str, Any]) -> str:
    calls: list[str] = []
    for part in list_value(payload.get("output")):
        part_map = mapping(part)
        if part_map.get("type") == "tool_call":
            calls.append(tool_call_display(ToolCall(text(part_map.get("tool_name")) or "tool", dict(mapping(part_map.get("input"))))))
    return ", ".join(calls)


def model_tool_requests_have_results(run: Run, model_step_index: int) -> bool:
    for step in run.completed_steps.values():
        if text(step.get("kind")) == "tool" and any(mapping(ref).get("step_index") == model_step_index for ref in list_value(step.get("input"))):
            return True
    return False


def flow_projection_lines(run: Run) -> list[str]:
    flow_steps = [
        payload
        for payload in [*run.completed_steps.values(), *(block.payload for block in run.steps.values())]
        if text(payload.get("kind")) in {"step", "parallel", "bind", "run"}
    ]
    lines: list[str] = []
    for payload in sorted(flow_steps, key=step_index):
        lines.extend(flow_step_lines(payload))
    return lines


def flow_step_lines(payload: Mapping[str, Any]) -> list[str]:
    data = mapping(payload.get("payload")) or mapping(payload.get("metadata"))
    title = text(data.get("stage_title")) or text(data.get("op")) or text(payload.get("kind")) or "stage"
    index = int_or_none(data.get("stage_index"))
    prefix = f"[{index + 1}] " if index is not None else ""
    return [f"{prefix}{title}", ""]


def tool_input_summary(value: Mapping[str, Any]) -> str:
    for key in ("command", "query", "path", "url"):
        if key in value:
            return plain_value(value[key])
    if not value:
        return ""
    return ", ".join(f"{key}={plain_value(item)}" for key, item in list(value.items())[:3])


def block_index(payload: Mapping[str, Any]) -> int:
    return step_index(payload) if "step_index" in payload else command_index(payload)


def command_index(payload: Mapping[str, Any]) -> int:
    ref = mapping(payload.get("ref"))
    return int_or_none(ref.get("index")) or int_or_none(payload.get("index")) or 0


def step_index(payload: Mapping[str, Any]) -> int:
    return int_or_none(payload.get("step_index")) or 0


def part_index(payload: Mapping[str, Any]) -> int:
    return int_or_none(payload.get("part_index")) or 0


def event_message_text(message: object) -> str:
    data = mapping(message)
    if not data:
        return ""
    parts = list_value(data.get("parts"))
    return "\n".join(text(mapping(part).get("text")) or "" for part in parts if mapping(part).get("type") == "text")


def event_parts_text(parts: object) -> str:
    return "\n".join(text(mapping(part).get("text")) or "" for part in list_value(parts) if mapping(part).get("type") == "text")


def display_status(value: object) -> str:
    status = text(value) or ""
    return {"finished": "succeeded", "cancelled": "canceled"}.get(status, status)


def plain_value(value: object) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    return str(value)


def mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def list_value(value: object) -> list[Any]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, str) else []


def text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def int_or_none(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None
