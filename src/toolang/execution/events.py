"""Execution trace events and full-message projections."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from toolang.base.types.message import (
    Delta,
    Message,
    MessageRole,
    Part,
    PartType,
    TextDelta,
    ToolCallDelta,
    part_from_data,
    parts_from_data,
    parts_to_data,
)
from .records import (
    CommandApply,
    CommandRecord,
    InputRef,
    OutputRef,
    RunRecord,
    RunStatus,
    StepInputItem,
    StepKind,
    StepPath,
    StepRecord,
    StepStatus,
    input_ref_from_data,
    output_ref_from_data,
    step_input_items_from_data,
    trace_index,
    trace_run,
)


@dataclass(frozen=True, slots=True)
class MessageData:
    """One full caller-facing message payload."""

    id: str
    thread_id: str
    run_id: str
    step_index: int
    role: MessageRole
    parts: list[Part] = field(default_factory=list)
    created_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> "MessageData":
        parts_payload = payload.get("parts")
        serialized_parts = (
            [item for item in parts_payload if isinstance(item, Mapping)]
            if isinstance(parts_payload, Sequence)
            else []
        )
        meta = payload.get("meta")
        return cls(
            id=str(payload.get("id", "")),
            thread_id=str(payload.get("thread_id", "")),
            run_id=str(payload.get("run_id", "")),
            step_index=int(payload.get("step_index", 0)),
            role=_message_role(payload.get("role")),
            parts=list(parts_from_data(serialized_parts)),
            created_at=str(payload.get("created_at", "")),
            meta=dict(meta) if isinstance(meta, Mapping) else {},
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "step_index": self.step_index,
            "role": self.role,
            "parts": parts_to_data(self.parts),
            "created_at": self.created_at,
            "meta": dict(self.meta),
        }


@dataclass(frozen=True, slots=True)
class RunWaiting:
    """One accepted run is waiting before execution starts."""

    run: str
    cmd: int
    parent: StepPath | None
    thread: str
    input: Message
    context: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    type: str = "run_waiting"


@dataclass(frozen=True, slots=True)
class RunStarting:
    """One accepted start command is entering execution."""

    run: str
    cmd: int
    parent: StepPath | None
    thread: str
    input: Message
    context: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    type: str = "run_starting"


@dataclass(frozen=True, slots=True)
class RunSteering:
    """One accepted steer command is entering the run stream."""

    run: str
    cmd: int
    input: Message
    apply: CommandApply
    context: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    type: str = "run_steering"


@dataclass(frozen=True, slots=True)
class RunStopping:
    """One accepted stop command is entering the run stream."""

    run: str
    cmd: int
    apply: CommandApply
    input: Message | None = None
    context: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    type: str = "run_stopping"


@dataclass(frozen=True, slots=True)
class RunBegin:
    """One run_begin trace event."""

    run: str
    input: InputRef
    context: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    type: str = "run_begin"


@dataclass(frozen=True, slots=True)
class StepBegin:
    """One step_begin trace event."""

    step: StepPath
    kind: StepKind
    input: tuple[StepInputItem, ...] = ()
    context: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    type: str = "step_begin"


@dataclass(frozen=True, slots=True)
class PartBegin:
    """One part_begin trace event."""

    step: StepPath
    part: int
    type_: PartType
    type: str = "part_begin"


@dataclass(frozen=True, slots=True)
class PartDelta:
    """One part_delta trace event."""

    step: StepPath
    part: int
    delta: Delta
    type: str = "part_delta"


@dataclass(frozen=True, slots=True)
class PartEnd:
    """One part_end trace event."""

    step: StepPath
    part: int
    data: Part
    type: str = "part_end"


@dataclass(frozen=True, slots=True)
class StepEnd:
    """One step_end trace event."""

    step: StepPath
    kind: StepKind
    status: StepStatus
    output: tuple[Part, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    finished_at: str = ""
    started_at: str = ""
    type: str = "step_end"


@dataclass(frozen=True, slots=True)
class RunEnd:
    """One run_end trace event."""

    run: str
    status: RunStatus
    input: InputRef | None = None
    output: OutputRef | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    finished_at: str = ""
    type: str = "run_end"


TraceEvent = (
    RunWaiting
    | RunStarting
    | RunSteering
    | RunStopping
    | RunBegin
    | StepBegin
    | PartBegin
    | PartDelta
    | PartEnd
    | StepEnd
    | RunEnd
)
TraceEventHandler = Callable[[TraceEvent], None]


def trace_event_from_data(data: Mapping[str, Any]) -> TraceEvent:
    """Return one trace event from public stream data."""

    event_type = str(data.get("type") or data.get("event_type") or "").strip()
    payload = data.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("trace event payload must be an object")
    if event_type == "run_waiting":
        return RunWaiting(
            run=str(payload.get("run", "")),
            cmd=int(payload.get("cmd", 0)),
            parent=_optional_text(payload.get("parent")),
            thread=str(payload.get("thread", "")),
            input=Message.from_data(_mapping_payload(payload.get("input"))),
            context=_dict_payload(payload.get("context")),
            created_at=str(payload.get("created_at", "")),
        )
    if event_type == "run_starting":
        return RunStarting(
            run=str(payload.get("run", "")),
            cmd=int(payload.get("cmd", 0)),
            parent=_optional_text(payload.get("parent")),
            thread=str(payload.get("thread", "")),
            input=Message.from_data(_mapping_payload(payload.get("input"))),
            context=_dict_payload(payload.get("context")),
            created_at=str(payload.get("created_at", "")),
        )
    if event_type == "run_steering":
        return RunSteering(
            run=str(payload.get("run", "")),
            cmd=int(payload.get("cmd", 0)),
            input=Message.from_data(_mapping_payload(payload.get("input"))),
            apply=_command_apply(payload.get("apply")),
            context=_dict_payload(payload.get("context")),
            created_at=str(payload.get("created_at", "")),
        )
    if event_type == "run_stopping":
        return RunStopping(
            run=str(payload.get("run", "")),
            cmd=int(payload.get("cmd", 0)),
            apply=_command_apply(payload.get("apply")),
            input=(
                Message.from_data(_mapping_payload(payload.get("input")))
                if isinstance(payload.get("input"), Mapping)
                else None
            ),
            context=_dict_payload(payload.get("context")),
            created_at=str(payload.get("created_at", "")),
        )
    if event_type == "run_begin":
        return RunBegin(
            run=str(payload.get("run", "")),
            input=input_ref_from_data(_mapping_payload(payload.get("input"))),
            context=_dict_payload(payload.get("context")),
            started_at=str(payload.get("started_at", "")),
        )
    if event_type == "step_begin":
        return StepBegin(
            step=str(payload.get("step", "")),
            kind=_step_kind(payload.get("kind")),
            input=step_input_items_from_data(
                tuple(
                    item
                    for item in payload.get("input", ())
                    if isinstance(item, Mapping)
                )
            )
            if isinstance(payload.get("input"), Sequence)
            else (),
            context=_dict_payload(payload.get("context")),
            started_at=str(payload.get("started_at") or payload.get("created_at") or ""),
        )
    if event_type == "part_begin":
        return PartBegin(
            step=str(payload.get("step", "")),
            part=int(payload.get("part", 0)),
            type_=_part_type(payload.get("type")),
        )
    if event_type == "part_delta":
        return PartDelta(
            step=str(payload.get("step", "")),
            part=int(payload.get("part", 0)),
            delta=_delta_from_data(_mapping_payload(payload.get("delta"))),
        )
    if event_type == "part_end":
        return PartEnd(
            step=str(payload.get("step", "")),
            part=int(payload.get("part", 0)),
            data=part_from_data(_mapping_payload(payload.get("data"))),
        )
    if event_type == "step_end":
        output_payload = payload.get("output")
        output = (
            parts_from_data([item for item in output_payload if isinstance(item, Mapping)])
            if isinstance(output_payload, Sequence)
            and not isinstance(output_payload, (str, bytes, bytearray))
            else ()
        )
        return StepEnd(
            step=str(payload.get("step", "")),
            kind=_step_kind(payload.get("kind")),
            status=_step_status(payload.get("status")),
            output=output,
            detail=_dict_payload(payload.get("detail")),
            error=_optional_text(payload.get("error")),
            started_at=str(payload.get("started_at", "")),
            finished_at=str(payload.get("finished_at") or payload.get("at") or ""),
        )
    if event_type == "run_end":
        return RunEnd(
            run=str(payload.get("run", "")),
            status=_run_status(payload.get("status")),
            input=(
                input_ref_from_data(_mapping_payload(payload.get("input")))
                if isinstance(payload.get("input"), Mapping)
                else None
            ),
            output=(
                output_ref_from_data(_mapping_payload(payload.get("output")))
                if isinstance(payload.get("output"), Mapping)
                else None
            ),
            detail=_dict_payload(payload.get("detail")),
            error=_optional_text(payload.get("error")),
            finished_at=str(payload.get("finished_at") or payload.get("at") or ""),
        )
    raise ValueError(f"unknown trace event type: {event_type or '<empty>'}")


def run_input_message_data(run: RunRecord, input: CommandRecord) -> MessageData:
    """Return one durable run input message."""

    if input.input is None:
        raise ValueError(f"run input has no message: {run.id}:{input.index}")
    message = input.input
    meta = dict(message.meta)
    meta.update({"kind": input.kind, "command_index": input.index})
    meta.update(dict(input.context))
    return MessageData(
        id=f"{run.id}:command:{input.index}",
        thread_id=run.thread,
        run_id=run.id,
        step_index=input.index,
        role=message.role,
        parts=list(message.parts),
        created_at=input.created_at,
        meta=meta,
    )


def run_input_record_message_data(
    run: RunRecord, input: CommandRecord
) -> MessageData | None:
    """Return the caller-facing message for one run input."""

    if input.input is None:
        return None
    return run_input_message_data(run, input)


def step_message_data(run: RunRecord, step: StepRecord) -> MessageData | None:
    """Build one caller-facing message from one durable step."""

    return message_data_for_step(
        step=step.path,
        thread=run.thread,
        kind=step.kind,
        output=step.output,
        created_at=step.finished_at or step.started_at,
        error=step.error,
    )


def run_output_message_data(
    *, run: RunRecord, steps: Sequence[StepRecord]
) -> MessageData | None:
    """Return the final assistant message for one run when present."""

    for step in reversed(steps):
        if step.kind == "model":
            return step_message_data(run, step)
    return None


def run_message_data(
    run: RunRecord,
    *,
    inputs: Sequence[CommandRecord],
    steps: Sequence[StepRecord],
) -> list[MessageData]:
    """Return the derived run transcript view."""

    messages = [
        message
        for input in inputs
        if (message := run_input_record_message_data(run, input)) is not None
    ]
    for step in steps:
        message = step_message_data(run, step)
        if message is not None:
            messages.append(message)
    return sorted(messages, key=lambda item: item.created_at)


def replay_message(message: MessageData) -> Message:
    """Return one model-history message reconstructed from one full message payload."""

    return Message(
        role=message.role,
        parts=tuple(message.parts),
        meta=dict(message.meta),
    )


def message_data_for_step(
    *,
    step: StepPath,
    thread: str,
    kind: StepKind,
    output: Sequence[Part],
    created_at: str,
    error: str | None = None,
) -> MessageData | None:
    """Build one caller-facing message from one step output."""

    if not output:
        return None
    role = _role_for_step(kind)
    if role is None:
        return None
    meta: dict[str, Any] = {}
    if error is not None:
        meta["error"] = error
    return MessageData(
        id=f"{step}:message",
        thread_id=thread,
        run_id=trace_run(step),
        step_index=trace_index(step) or 0,
        role=role,
        parts=list(output),
        created_at=created_at,
        meta=meta,
    )


def provider_metadata(name: str) -> dict[str, Any]:
    """Return one provider metadata mapping for one tool name."""

    return {
        "toolang": {
            "toolFamily": name,
            "toolName": name,
        }
    }


def _delta_from_data(payload: Mapping[str, Any]) -> Delta:
    kind = str(payload.get("type") or payload.get("kind") or "").strip()
    if kind == "tool_call":
        return ToolCallDelta(
            text=str(payload.get("text", "")),
            tool_call_id=str(payload.get("tool_call_id", "")),
        )
    return TextDelta(text=str(payload.get("text", "")))


def _mapping_payload(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _dict_payload(value: object) -> dict[str, Any]:
    return dict(cast(Mapping[str, Any], value)) if isinstance(value, Mapping) else {}


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _run_status(value: object) -> RunStatus:
    text = str(value or "finished")
    if text == "succeeded":
        text = "finished"
    if text in {"pending", "running", "finished", "failed", "canceled"}:
        return cast(RunStatus, text)
    return "failed"


def _step_status(value: object) -> StepStatus:
    text = str(value or "finished")
    if text in {"running", "finished", "failed", "canceled"}:
        return cast(StepStatus, text)
    return "failed"


def _step_kind(value: object) -> StepKind:
    text = str(value or "system")
    if text in {
        "run",
        "agent",
        "human",
        "model",
        "tool",
        "par",
        "loop",
        "system",
    }:
        return cast(StepKind, text)
    return "system"


def _command_apply(value: object) -> CommandApply:
    text = str(value or "now")
    if text in {"now", "next_step", "next_call"}:
        return cast(CommandApply, text)
    raise ValueError(f"unsupported command apply mode: {text}")


def _part_type(value: object) -> PartType:
    text = str(value or "text")
    if text in {"text", "image", "audio", "file", "tool_call", "tool_result"}:
        return cast(PartType, text)
    return "text"


def _role_for_step(kind: StepKind) -> MessageRole | None:
    if kind == "model":
        return "assistant"
    if kind == "tool":
        return "tool"
    return None


def _message_role(value: object) -> MessageRole:
    text = str(value or "user").strip()
    if text not in {"user", "assistant", "tool"}:
        raise ValueError(f"unsupported message role: {text or '<empty>'}")
    return cast(MessageRole, text)
