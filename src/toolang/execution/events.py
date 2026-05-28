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
    parts_from_data,
    parts_to_data,
)
from .records import (
    InputRecord,
    RunRecord,
    RunStatus,
    StepInputItem,
    StepKind,
    StepPayload,
    StepRecord,
    StepStatus,
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
class RunStart:
    """One run-start trace event."""

    run_id: str
    origin: str
    thread_id: str
    input: Message
    created_at: str
    started_at: str
    request_id: str | None = None
    type: str = "run-start"


@dataclass(frozen=True, slots=True)
class StepStart:
    """One step-start trace event."""

    run_id: str
    thread_id: str
    step_index: int
    kind: StepKind
    input: tuple[StepInputItem, ...]
    started_at: str
    instruct: str | None = None
    context: str | None = None
    type: str = "step-start"


@dataclass(frozen=True, slots=True)
class PartStart:
    """One part-start trace event."""

    run_id: str
    thread_id: str
    step_index: int
    part_index: int
    kind: PartType
    type: str = "part-start"


@dataclass(frozen=True, slots=True)
class PartDelta:
    """One part-delta trace event."""

    run_id: str
    thread_id: str
    step_index: int
    part_index: int
    delta: Delta
    type: str = "part-delta"


@dataclass(frozen=True, slots=True)
class PartEnd:
    """One part-end trace event."""

    run_id: str
    thread_id: str
    step_index: int
    part_index: int
    data: Part
    type: str = "part-end"


@dataclass(frozen=True, slots=True)
class StepEnd:
    """One step-end trace event."""

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
    type: str = "step-end"


@dataclass(frozen=True, slots=True)
class RunEnd:
    """One run-end trace event."""

    run_id: str
    thread_id: str
    status: RunStatus
    finished_at: str
    error: str | None = None
    type: str = "run-end"


TraceEvent = RunStart | StepStart | PartStart | PartDelta | PartEnd | StepEnd | RunEnd
TraceEventHandler = Callable[[TraceEvent], None]


def run_input_message_data(run: RunRecord, input: InputRecord) -> MessageData:
    """Return one durable run input message."""

    if input.message is None:
        raise ValueError(f"run input has no message: {run.run_id}:{input.index}")
    message = input.message
    meta = dict(message.meta)
    meta.update({"action": input.action, "input_index": input.index})
    if input.mode is not None:
        meta["mode"] = input.mode
    if input.request_id is not None:
        meta["request_id"] = input.request_id
    return MessageData(
        id=f"{run.run_id}:input:{input.index}",
        thread_id=run.thread_id,
        run_id=run.run_id,
        step_index=input.index,
        role=message.role,
        parts=list(message.parts),
        created_at=input.created_at,
        meta=meta,
    )


def run_input_record_message_data(run: RunRecord, input: InputRecord) -> MessageData | None:
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


def run_output_message_data(*, run: RunRecord, steps: Sequence[StepRecord]) -> MessageData | None:
    """Return the final assistant message for one run when present."""

    for step in reversed(steps):
        if step.kind == "model_call":
            return step_message_data(run, step)
    return None


def run_message_data(
    run: RunRecord,
    *,
    inputs: Sequence[InputRecord],
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
    if kind == "model_call":
        return "assistant"
    if kind == "tool_call":
        return "tool"
    return None


def _message_id(run_id: str, step_index: int) -> str:
    return f"{run_id}:step:{step_index}"


def _message_role(value: object) -> MessageRole:
    text = str(value or "user").strip()
    if text not in {"user", "assistant", "tool"}:
        raise ValueError(f"unsupported message role: {text or '<empty>'}")
    return cast(MessageRole, text)
