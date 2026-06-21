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
    CommandMode,
    CommandRef,
    CommandRecord,
    RunRecord,
    RunStatus,
    StepInputItem,
    StepKind,
    StepOutputRef,
    StepPayload,
    StepRecord,
    StepStatus,
    step_payload_from_data,
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
    def from_data(cls, payload: Mapping[str, Any]) -> MessageData:
        """Build one message payload from one serialized mapping."""

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
        """Return one serialized message payload."""

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
class RunStarting:
    """One accepted start-command trace event."""

    run_id: str
    origin: str
    thread_id: str | None
    input: Message
    accepted_at: str
    request_id: str | None = None
    type: str = "run_starting"


@dataclass(frozen=True, slots=True)
class RunWaiting:
    """One run_waiting queue progress event."""

    run_id: str
    thread_id: str | None
    origin: str
    reason: str
    created_at: str
    position: int | None = None
    request_id: str | None = None
    group: str | None = None
    executable_kind: str | None = None
    executable_name: str | None = None
    type: str = "run_waiting"


@dataclass(frozen=True, slots=True)
class RunSteering:
    """One accepted steer-command trace event."""

    run_id: str
    thread_id: str
    index: int
    message: Message
    accepted_at: str
    mode: CommandMode | None = None
    request_id: str | None = None
    type: str = "run_steering"


@dataclass(frozen=True, slots=True)
class RunStopping:
    """One accepted stop-command trace event."""

    run_id: str
    thread_id: str
    index: int
    accepted_at: str
    mode: CommandMode | None = None
    request_id: str | None = None
    reason: str | None = None
    type: str = "run_stopping"


@dataclass(frozen=True, slots=True)
class RunBegin:
    """One run_begin trace event."""

    run_id: str
    origin: str
    thread_id: str
    input: Message
    created_at: str
    started_at: str
    request_id: str | None = None
    root_run_id: str | None = None
    parent_run_id: str | None = None
    parent_step_index: int | None = None
    executable_kind: str = "thunk"
    executable_name: str | None = None
    call_kind: str = "top"
    metadata: dict[str, Any] = field(default_factory=dict)
    type: str = "run_begin"


@dataclass(frozen=True, slots=True)
class StepBegin:
    """One step_begin trace event."""

    run_id: str
    thread_id: str
    step_index: int
    kind: StepKind
    input: tuple[StepInputItem, ...]
    started_at: str
    instruct: str | None = None
    context: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    type: str = "step_begin"


@dataclass(frozen=True, slots=True)
class PartBegin:
    """One part_begin trace event."""

    run_id: str
    thread_id: str
    step_index: int
    part_index: int
    kind: PartType
    type: str = "part_begin"


@dataclass(frozen=True, slots=True)
class PartDelta:
    """One part_delta trace event."""

    run_id: str
    thread_id: str
    step_index: int
    part_index: int
    delta: Delta
    type: str = "part_delta"


@dataclass(frozen=True, slots=True)
class PartEnd:
    """One part_end trace event."""

    run_id: str
    thread_id: str
    step_index: int
    part_index: int
    data: Part
    type: str = "part_end"


@dataclass(frozen=True, slots=True)
class StepEnd:
    """One step_end trace event."""

    run_id: str
    thread_id: str
    step_index: int
    kind: StepKind
    status: StepStatus
    output: tuple[Part, ...]
    payload: StepPayload
    started_at: str
    finished_at: str
    error: str | None = None
    type: str = "step_end"


@dataclass(frozen=True, slots=True)
class RunEnd:
    """One run_end trace event."""

    run_id: str
    thread_id: str
    status: RunStatus
    finished_at: str
    error: str | None = None
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
        return _run_waiting_from_payload(payload)
    if event_type == "run_starting":
        return _run_starting_from_payload(payload)
    if event_type == "run_steering":
        return _run_steering_from_payload(payload)
    if event_type == "run_stopping":
        return _run_stopping_from_payload(payload)
    if event_type == "run_begin":
        return _run_begin_from_payload(payload)
    if event_type == "step_begin":
        return _step_begin_from_payload(payload)
    if event_type == "part_begin":
        return PartBegin(
            run_id=str(payload.get("run_id", "")),
            thread_id=str(payload.get("thread_id", "")),
            step_index=int(payload.get("step_index", 0)),
            part_index=int(payload.get("part_index", 0)),
            kind=_part_type(payload.get("kind")),
        )
    if event_type == "part_delta":
        return PartDelta(
            run_id=str(payload.get("run_id", "")),
            thread_id=str(payload.get("thread_id", "")),
            step_index=int(payload.get("step_index", 0)),
            part_index=int(payload.get("part_index", 0)),
            delta=_delta_from_data(_mapping_payload(payload.get("delta"))),
        )
    if event_type == "part_end":
        part_payload = payload.get("part", payload.get("data"))
        return PartEnd(
            run_id=str(payload.get("run_id", "")),
            thread_id=str(payload.get("thread_id", "")),
            step_index=int(payload.get("step_index", 0)),
            part_index=int(payload.get("part_index", 0)),
            data=part_from_data(_mapping_payload(part_payload)),
        )
    if event_type == "step_end":
        return _step_end_from_payload(payload)
    if event_type == "run_end":
        return RunEnd(
            run_id=str(payload.get("run_id", "")),
            thread_id=str(payload.get("thread_id", "")),
            status=_run_status(payload.get("status")),
            finished_at=str(payload.get("finished_at") or payload.get("at") or ""),
            error=_optional_text(payload.get("error")),
        )
    raise ValueError(f"unknown trace event type: {event_type or '<empty>'}")


def run_input_message_data(run: RunRecord, input: CommandRecord) -> MessageData:
    """Return one durable run input message."""

    if input.message is None:
        raise ValueError(f"run input has no message: {run.run_id}:{input.index}")
    message = input.message
    meta = dict(message.meta)
    meta.update({"kind": input.kind, "command_index": input.index})
    if input.mode is not None:
        meta["mode"] = input.mode
    if input.request_id is not None:
        meta["request_id"] = input.request_id
    return MessageData(
        id=f"{run.run_id}:command:{input.index}",
        thread_id=run.thread_id,
        run_id=run.run_id,
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

    if input.message is None:
        return None
    return run_input_message_data(run, input)


def step_message_data(run: RunRecord, step: StepRecord) -> MessageData | None:
    """Build one caller-facing message from one durable step."""

    return message_data_for_step(
        run_id=run.run_id,
        thread_id=run.thread_id,
        step_index=step.step_index,
        kind=step.kind,
        output=step.output,
        created_at=step.finished_at,
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


def _run_waiting_from_payload(payload: Mapping[str, Any]) -> RunWaiting:
    return RunWaiting(
        run_id=str(payload.get("run_id", "")),
        thread_id=_optional_text(payload.get("thread_id")),
        origin=str(payload.get("origin", "")),
        reason=str(payload.get("reason") or "queue"),
        created_at=str(payload.get("created_at") or payload.get("at") or ""),
        position=_optional_int(payload.get("position")),
        request_id=_optional_text(payload.get("request_id")),
        group=_optional_text(payload.get("group")),
        executable_kind=_optional_text(payload.get("executable_kind")),
        executable_name=_optional_text(payload.get("executable_name")),
    )


def _run_starting_from_payload(payload: Mapping[str, Any]) -> RunStarting:
    return RunStarting(
        run_id=str(payload.get("run_id", "")),
        origin=str(payload.get("origin", "")),
        thread_id=_optional_text(payload.get("thread_id")),
        input=Message.from_data(
            _mapping_payload(payload.get("input") or payload.get("message"))
        ),
        accepted_at=str(
            payload.get("accepted_at")
            or payload.get("created_at")
            or payload.get("at")
            or ""
        ),
        request_id=_optional_text(payload.get("request_id")),
    )


def _run_steering_from_payload(payload: Mapping[str, Any]) -> RunSteering:
    return RunSteering(
        run_id=str(payload.get("run_id", "")),
        thread_id=str(payload.get("thread_id", "")),
        index=_command_index(payload),
        message=Message.from_data(_mapping_payload(payload.get("message"))),
        accepted_at=str(
            payload.get("accepted_at")
            or payload.get("created_at")
            or payload.get("at")
            or ""
        ),
        mode=_command_mode(payload.get("mode")),
        request_id=_optional_text(payload.get("request_id")),
    )


def _run_stopping_from_payload(payload: Mapping[str, Any]) -> RunStopping:
    return RunStopping(
        run_id=str(payload.get("run_id", "")),
        thread_id=str(payload.get("thread_id", "")),
        index=_command_index(payload),
        accepted_at=str(
            payload.get("accepted_at")
            or payload.get("created_at")
            or payload.get("at")
            or ""
        ),
        mode=_command_mode(payload.get("mode")),
        request_id=_optional_text(payload.get("request_id")),
        reason=_optional_text(payload.get("reason")),
    )


def _run_begin_from_payload(payload: Mapping[str, Any]) -> RunBegin:
    return RunBegin(
        run_id=str(payload.get("run_id", "")),
        origin=str(payload.get("origin", "")),
        thread_id=str(payload.get("thread_id", "")),
        input=Message.from_data(
            _mapping_payload(payload.get("input") or payload.get("message"))
        ),
        created_at=str(payload.get("created_at", "")),
        started_at=str(payload.get("started_at") or payload.get("created_at") or ""),
        request_id=_optional_text(payload.get("request_id")),
        root_run_id=_optional_text(payload.get("root_run_id")),
        parent_run_id=_optional_text(payload.get("parent_run_id")),
        parent_step_index=_optional_int(payload.get("parent_step_index")),
        executable_kind=str(payload.get("executable_kind", "thunk")),
        executable_name=_optional_text(payload.get("executable_name")),
        call_kind=str(payload.get("call_kind", "top")),
        metadata=_dict_payload(payload.get("metadata")),
    )


def _step_begin_from_payload(payload: Mapping[str, Any]) -> StepBegin:
    kind = _step_kind(payload.get("kind"))
    return StepBegin(
        run_id=str(payload.get("run_id", "")),
        thread_id=str(payload.get("thread_id", "")),
        step_index=int(payload.get("step_index", 0)),
        kind=kind,
        input=_step_input_items(payload.get("input")),
        started_at=str(payload.get("started_at") or payload.get("created_at") or ""),
        instruct=_optional_text(payload.get("instruct")),
        context=_optional_text(payload.get("context")),
        metadata=_dict_payload(payload.get("metadata")),
    )


def _step_end_from_payload(payload: Mapping[str, Any]) -> StepEnd:
    kind = _step_kind(payload.get("kind"))
    output_payload = payload.get("output")
    output = (
        parts_from_data([item for item in output_payload if isinstance(item, Mapping)])
        if isinstance(output_payload, Sequence)
        and not isinstance(output_payload, (str, bytes, bytearray))
        else ()
    )
    return StepEnd(
        run_id=str(payload.get("run_id", "")),
        thread_id=str(payload.get("thread_id", "")),
        step_index=int(payload.get("step_index", 0)),
        kind=kind,
        status=_step_status(payload.get("status")),
        output=output,
        payload=step_payload_from_data(kind, _mapping_payload(payload.get("payload"))),
        started_at=str(payload.get("started_at", "")),
        finished_at=str(payload.get("finished_at") or payload.get("at") or ""),
        error=_optional_text(payload.get("error")),
    )


def _step_input_items(value: object) -> tuple[StepInputItem, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    items: list[StepInputItem] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        payload = cast(Mapping[str, Any], item)
        kind = str(payload.get("kind", "")).strip()
        if kind == "command":
            items.append(CommandRef.from_data(payload))
        elif kind == "step":
            items.append(StepOutputRef.from_data(payload))
        elif "parts" in payload:
            items.append(Message.from_data(payload))
    return tuple(items)


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


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _command_index(payload: Mapping[str, Any]) -> int:
    ref = payload.get("ref")
    if isinstance(ref, Mapping):
        return int(ref.get("index", 0) or 0)
    return int(payload.get("index", 0) or 0)


def _command_mode(value: object) -> CommandMode | None:
    if value in {"immediate", "next_step", "next_call"}:
        return cast(CommandMode, value)
    return None


def _run_status(value: object) -> RunStatus:
    text = str(value or "finished")
    if text == "succeeded":
        text = "finished"
    if text in {"running", "finished", "failed", "canceled"}:
        return cast(RunStatus, text)
    return "failed"


def _step_status(value: object) -> StepStatus:
    text = str(value or "finished")
    if text in {"finished", "failed", "canceled"}:
        return cast(StepStatus, text)
    return "failed"


def _step_kind(value: object) -> StepKind:
    text = str(value or "system")
    if text in {"model", "tool", "agent", "run", "step", "parallel", "bind", "system"}:
        return cast(StepKind, text)
    return "system"


def _part_type(value: object) -> PartType:
    text = str(value or "text")
    if text in {"text", "image", "audio", "file", "tool_call", "tool_result"}:
        return cast(PartType, text)
    return "text"


def message_data_for_step(
    *,
    run_id: str,
    thread_id: str,
    step_index: int,
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
        id=_message_id(run_id, step_index),
        thread_id=thread_id,
        run_id=run_id,
        step_index=step_index,
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


def _role_for_step(kind: StepKind) -> MessageRole | None:
    if kind == "model":
        return "assistant"
    if kind == "tool":
        return "tool"
    return None


def _message_id(run_id: str, step_index: int) -> str:
    return f"{run_id}:step:{step_index}"


def _message_role(value: object) -> MessageRole:
    text = str(value or "user").strip()
    if text not in {"user", "assistant", "tool"}:
        raise ValueError(f"unsupported message role: {text or '<empty>'}")
    return cast(MessageRole, text)
