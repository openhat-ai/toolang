"""Durable execution record types."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from toolang.base.types.message import Message, Part
from .types import (
    CommandApply,
    CommandKind,
    CommandStatus,
    EventDomain,
    RunId,
    RunStatus,
    StepKind,
    StepPath,
    StepStatus,
    ThreadPeerType,
    UpdateKind,
)


@dataclass(frozen=True, slots=True)
class InputRef:
    """Reference one run command input or one command input part."""

    cmd: int = 0
    part: int | None = None

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> "InputRef":
        return cls(
            cmd=int(payload.get("cmd", 0) or 0),
            part=_optional_int(payload.get("part")),
        )

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"cmd": self.cmd}
        if self.part is not None:
            data["part"] = self.part
        return data


@dataclass(frozen=True, slots=True)
class OutputRef:
    """Reference one step output or one step output part."""

    step: StepPath
    part: int | None = None

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> "OutputRef":
        return cls(
            step=str(payload.get("step", "")),
            part=_optional_int(payload.get("part")),
        )

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"step": self.step}
        if self.part is not None:
            data["part"] = self.part
        return data


StepInputItem = InputRef | OutputRef | Message


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Durable run truth."""

    id: RunId
    parent: StepPath | None
    thread: str
    input: InputRef
    output: OutputRef | None
    context: dict[str, Any] = field(default_factory=dict)
    status: RunStatus = "pending"
    error: str | None = None
    created_at: str = ""
    started_at: str = ""
    finished_at: str | None = None

    @property
    def run_id(self) -> str:
        return self.id

    @property
    def thread_id(self) -> str:
        return self.thread

    @property
    def root_run_id(self) -> str:
        value = self.context.get("root")
        return str(value) if value is not None else self.id

    @property
    def origin(self) -> str:
        value = self.context.get("origin")
        return str(value) if value is not None else ""

    @property
    def executable_kind(self) -> str:
        executable = self.context.get("executable")
        if isinstance(executable, Mapping):
            return str(executable.get("kind") or "")
        return ""

    @property
    def executable_name(self) -> str | None:
        executable = self.context.get("executable")
        if isinstance(executable, Mapping) and executable.get("name") is not None:
            return str(executable.get("name"))
        return None

    @property
    def call_kind(self) -> str:
        value = self.context.get("call")
        return str(value) if value is not None else ""

    @property
    def superseded(self) -> dict[str, Any] | None:
        value = self.context.get("superseded")
        return dict(value) if isinstance(value, Mapping) else None

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(self.context)


@dataclass(frozen=True, slots=True)
class ThreadPeer:
    """One local thread peer descriptor."""

    type: ThreadPeerType = "user"
    name: str = "user"
    thread: str | None = None

    @classmethod
    def from_data(cls, payload: Mapping[str, Any] | None) -> "ThreadPeer":
        if payload is None:
            return cls()
        peer_type = str(payload.get("type", "user")).strip() or "user"
        if peer_type not in {"user", "agent"}:
            raise ValueError(f"unsupported thread peer type: {peer_type}")
        name = str(payload.get("name", "user" if peer_type == "user" else "")).strip()
        if peer_type == "agent" and not name:
            raise ValueError("agent thread peer requires name")
        raw_thread = payload.get("thread")
        thread = str(raw_thread).strip() if raw_thread is not None else None
        return cls(
            type=cast(ThreadPeerType, peer_type),
            name=name or "user",
            thread=thread or None,
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "name": self.name,
            "thread": self.thread,
        }


@dataclass(frozen=True, slots=True)
class ThreadRecord:
    """Durable thread metadata."""

    thread_id: str
    origin: str
    peer: ThreadPeer
    parent: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One durable execution step."""

    parent: StepPath
    index: int
    kind: StepKind
    input: tuple[StepInputItem, ...]
    output: tuple[Part, ...]
    context: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)
    status: StepStatus = "running"
    error: str | None = None
    created_at: str = ""
    started_at: str = ""
    finished_at: str | None = None

    @property
    def path(self) -> StepPath:
        return trace_child_path(self.parent, self.index)

    @property
    def run_id(self) -> str:
        return trace_run(self.parent)

    @property
    def step_index(self) -> int:
        return self.index

    @property
    def payload(self) -> dict[str, Any]:
        return dict(self.detail)


@dataclass(frozen=True, slots=True)
class UpdateRecord:
    """One agent-local operational update."""

    update_id: int
    kind: UpdateKind
    payload: dict[str, Any]
    created_at: str

    def to_data(self) -> dict[str, Any]:
        return {
            "id": self.update_id,
            "kind": self.kind,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One durable resource-scoped event."""

    event_id: int
    domain: EventDomain
    domain_id: str
    seq: int
    type: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class CommandRecord:
    """One durable command sent to a run."""

    run: RunId
    index: int
    kind: CommandKind
    apply: CommandApply
    input: Message | None
    context: dict[str, Any] = field(default_factory=dict)
    status: CommandStatus = "pending"
    error: str | None = None
    created_at: str = ""
    finished_at: str | None = None

    @property
    def run_id(self) -> str:
        return self.run


def trace_run(path: StepPath) -> RunId:
    """Return the run id component of one trace path."""

    return path.split("/", 1)[0]


def trace_parent(path: StepPath) -> StepPath | None:
    """Return the parent trace path for a step path."""

    if "/" not in path:
        return None
    return path.rsplit("/", 1)[0]


def trace_index(path: StepPath) -> int | None:
    """Return the leaf step index for a step path."""

    if "/" not in path:
        return None
    try:
        return int(path.rsplit("/", 1)[1])
    except ValueError:
        return None


def trace_child_path(parent: StepPath, index: int) -> StepPath:
    """Return a child step path under one trace path."""

    return f"{parent}/{index}"


def input_ref_from_data(payload: Mapping[str, Any] | None) -> InputRef:
    """Return one input ref from serialized data."""

    return InputRef.from_data(payload or {})


def input_ref_to_data(ref: InputRef) -> dict[str, Any]:
    """Return serialized input ref data."""

    return ref.to_data()


def output_ref_from_data(payload: Mapping[str, Any] | None) -> OutputRef | None:
    """Return one output ref from serialized data."""

    if not payload:
        return None
    return OutputRef.from_data(payload)


def output_ref_to_data(ref: OutputRef | None) -> dict[str, Any] | None:
    """Return serialized output ref data."""

    return ref.to_data() if ref is not None else None


def step_input_item_from_data(payload: Mapping[str, Any]) -> StepInputItem:
    """Return one step input item from one serialized payload."""

    if "cmd" in payload:
        return InputRef.from_data(payload)
    if "step" in payload:
        return OutputRef.from_data(payload)
    if "message" in payload:
        return Message.from_data(_mapping(payload.get("message")))
    if "role" in payload and "parts" in payload:
        return Message.from_data(payload)
    raise ValueError("unknown step input item shape")


def step_input_items_from_data(
    payloads: Sequence[Mapping[str, Any]],
) -> tuple[StepInputItem, ...]:
    """Return step input items from one serialized sequence."""

    return tuple(step_input_item_from_data(item) for item in payloads)


def step_input_item_to_data(item: StepInputItem) -> dict[str, Any]:
    """Return one serialized step input item."""

    if isinstance(item, InputRef):
        return item.to_data()
    if isinstance(item, OutputRef):
        return item.to_data()
    return {"message": item.to_data()}


def step_input_items_to_data(items: tuple[StepInputItem, ...]) -> list[dict[str, Any]]:
    """Return serialized step input items."""

    return [step_input_item_to_data(item) for item in items]


def step_input_messages(items: tuple[StepInputItem, ...]) -> tuple[Message, ...]:
    """Return inline input messages from one step input tuple."""

    return tuple(item for item in items if isinstance(item, Message))


def cast_message_role(role: str) -> Literal["user", "assistant", "tool"]:
    """Return one normalized message role literal."""

    if role not in {"user", "assistant", "tool"}:
        raise ValueError(f"unsupported message role: {role}")
    return cast(Literal["user", "assistant", "tool"], role)


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
