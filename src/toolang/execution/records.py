"""Durable execution record types."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from toolang.base.types.message import Message, MessagePart, MessageRole
from .types import (
    ControlStatus,
    ControlTiming,
    RunControlKind,
    RunId,
    RunStatus,
    StepKind,
    StepPath,
    StepStatus,
    ThreadPeerType,
    ThreadControlKind,
)


@dataclass(frozen=True, slots=True)
class RunControlRef:
    """Reference one control input within the current run."""

    index: int = 0
    part: int | None = None

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> "RunControlRef":
        return cls(
            index=int(payload.get("control", 0) or 0),
            part=_optional_int(payload.get("part")),
        )

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"control": self.index}
        if self.part is not None:
            data["part"] = self.part
        return data


@dataclass(frozen=True, slots=True)
class StepOutputRef:
    """Reference one step output or one step output part."""

    step: StepPath
    part: int | None = None

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> StepOutputRef:
        return cls(
            step=StepPath.parse(str(payload.get("step", ""))),
            part=_optional_int(payload.get("part")),
        )

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"step": str(self.step)}
        if self.part is not None:
            data["part"] = self.part
        return data

    def resolve(self, steps: Sequence[StepRecord]) -> tuple[MessagePart, ...]:
        """Resolve this durable output edge against available step records."""

        step = next((item for item in steps if item.path == self.step), None)
        if step is None:
            return ()
        if self.part is None:
            return step.output
        if 0 <= self.part < len(step.output):
            return (step.output[self.part],)
        return ()


ValueRef = RunControlRef | StepOutputRef
StepInput = ValueRef | Message


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Durable run truth."""

    id: RunId
    parent: StepPath | None
    thread: str
    input: RunControlRef
    output: ValueRef | None
    context: dict[str, Any] = field(default_factory=dict)
    status: RunStatus = "pending"
    error: str | None = None
    superseded_by: ThreadControlRef | None = None
    created_at: str = ""
    started_at: str = ""
    finished_at: str | None = None

    @property
    def root_run_id(self) -> str:
        value = self.context.get("root")
        return str(value) if value is not None else self.id

    @property
    def runnable_kind(self) -> str:
        runnable = self.context.get("runnable")
        if isinstance(runnable, Mapping):
            return str(runnable.get("kind") or "")
        return ""

    @property
    def runnable_name(self) -> str | None:
        runnable = self.context.get("runnable")
        if isinstance(runnable, Mapping) and runnable.get("name") is not None:
            return str(runnable.get("name"))
        return None

    @property
    def call_kind(self) -> str:
        value = self.context.get("call")
        return str(value) if value is not None else ""


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
    created_by: ThreadControlRef
    head: ThreadControlRef
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One durable execution step."""

    path: StepPath
    kind: StepKind
    input: tuple[StepInput, ...]
    output: tuple[MessagePart, ...]
    given: dict[str, Any] = field(default_factory=dict)
    noted: dict[str, Any] = field(default_factory=dict)
    status: StepStatus = "running"
    error: str | None = None
    created_at: str = ""
    started_at: str = ""
    finished_at: str | None = None

    @property
    def run_id(self) -> str:
        return self.path.run

    @property
    def parent(self) -> StepPath | None:
        return self.path.parent

    @property
    def index(self) -> int:
        return self.path.index


@dataclass(frozen=True, slots=True)
class RunControlRecord:
    """One durable control sent to a run."""

    run: RunId
    index: int
    kind: RunControlKind
    timing: ControlTiming
    input: Message | None
    request_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    status: ControlStatus = "pending"
    error: str | None = None
    created_at: str = ""
    finished_at: str | None = None


@dataclass(frozen=True, slots=True)
class ThreadControlRef:
    """Reference one durable thread control."""

    thread: str
    index: int


@dataclass(frozen=True, slots=True)
class ThreadControlRecord:
    """One durable mutation applied to a thread."""

    thread: str
    index: int
    kind: ThreadControlKind
    source_thread: str | None = None
    anchor_run: str | None = None
    request_id: str | None = None
    expected_head: ThreadControlRef | None = None
    context: dict[str, Any] = field(default_factory=dict)
    status: ControlStatus = "pending"
    created_at: str = ""
    finished_at: str | None = None


def value_ref_from_data(payload: Mapping[str, Any]) -> ValueRef:
    """Parse one durable value reference."""

    if "control" in payload:
        return RunControlRef.from_data(payload)
    if "step" in payload:
        return StepOutputRef.from_data(payload)
    raise ValueError("unknown value reference shape")


def value_ref_to_data(ref: ValueRef) -> dict[str, Any]:
    """Serialize one durable value reference."""

    return ref.to_data()


def step_input_from_data(payload: Mapping[str, Any]) -> StepInput:
    """Return one step input item from one serialized payload."""

    if "control" in payload or "step" in payload:
        return value_ref_from_data(payload)
    if "message" in payload:
        return Message.from_data(_mapping(payload.get("message")))
    if "role" in payload and "parts" in payload:
        return Message.from_data(payload)
    raise ValueError("unknown step input item shape")


def step_inputs_from_data(
    payloads: Sequence[Mapping[str, Any]],
) -> tuple[StepInput, ...]:
    """Return step input items from one serialized sequence."""

    return tuple(step_input_from_data(item) for item in payloads)


def step_input_to_data(item: StepInput) -> dict[str, Any]:
    """Return one serialized step input item."""

    if isinstance(item, RunControlRef | StepOutputRef):
        return value_ref_to_data(item)
    return {"message": item.to_data()}


def step_inputs_to_data(items: tuple[StepInput, ...]) -> list[dict[str, Any]]:
    """Return serialized step input items."""

    return [step_input_to_data(item) for item in items]


def step_message_role(kind: StepKind) -> MessageRole | None:
    """Return the transcript role produced by one step kind."""

    if kind == "model":
        return "assistant"
    if kind == "tool":
        return "tool"
    return None


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
