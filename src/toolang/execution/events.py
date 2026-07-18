"""Execution trace event types and serialization."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import threading
from typing import Any, cast

from toolang.base.types.message import (
    Delta,
    Message,
    Part,
    PartType,
    TextDelta,
    ToolCallDelta,
    part_from_data,
    parts_from_data,
)
from .records import (
    CommandApply,
    InputRef,
    OutputRef,
    RunStatus,
    StepInputItem,
    StepKind,
    StepPath,
    StepStatus,
    input_ref_from_data,
    output_ref_from_data,
    step_input_items_from_data,
)


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


def combine_trace_handlers(*handlers: TraceEventHandler) -> TraceEventHandler:
    """Return one ordered, thread-safe handler over trace projections."""

    lock = threading.Lock()

    def handle(event: TraceEvent) -> None:
        with lock:
            for handler in handlers:
                handler(event)

    return handle


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
