"""Durable execution record types."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic import TypeAdapter

from toolang.base.types.message import Message, MessagePart, MessageRole
from toolang.base.types.policy import RunBindings, RunLimits
from toolang.base.types.run import ModelCall
from toolang.lang.input import RunInput
from .types import (
    ControlStatus,
    ControlTiming,
    AgentResources,
    ExecutionError,
    RunControlKind,
    RunId,
    RunStatus,
    StepErrorRef,
    StepKind,
    StepPath,
    StepStatus,
    ThreadPeerType,
    ThreadControlKind,
)


@dataclass(frozen=True, slots=True)
class RunInputRef:
    """Reference one control input within the current run."""

    index: int = 0
    name: str | None = None
    part: int | None = None

    @classmethod
    def from_data(cls, payload: Mapping[str, Any]) -> "RunInputRef":
        return cls(
            index=int(payload.get("control", 0) or 0),
            name=(str(payload["name"]) if payload.get("name") is not None else None),
            part=_optional_int(payload.get("part")),
        )

    def to_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"control": self.index}
        if self.name is not None:
            data["name"] = self.name
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


ValueRef = RunInputRef | StepOutputRef
StepInput = ValueRef | Message

_MODEL_CALL_ADAPTER = TypeAdapter(ModelCall)
_RUN_BINDINGS_ADAPTER = TypeAdapter(RunBindings)
_RUN_LIMITS_ADAPTER = TypeAdapter(RunLimits)


@dataclass(frozen=True, slots=True)
class RunControlRef:
    """Reference one globally addressed run control."""

    run: RunId
    index: int


@dataclass(frozen=True, slots=True)
class ThreadControlRef:
    """Reference one durable thread control."""

    thread: str
    index: int


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Durable run truth."""

    id: RunId
    parent: StepPath | None
    thread: str
    input: RunInputRef
    output: ValueRef | None
    context: dict[str, Any] = field(default_factory=dict)
    status: RunStatus = "pending"
    error: ExecutionError | None = None
    ejected: ThreadControlRef | RunControlRef | None = None
    created_at: str = ""
    started_at: str = ""
    finished_at: str | None = None

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
    error: ExecutionError | None = None
    ejected: RunControlRef | None = None
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
    input: RunInput | None = None
    bindings: RunBindings | None = None
    limits: RunLimits | None = None
    resources: AgentResources | None = None
    message: Message | None = None
    reason: str | None = None
    source: RunId | None = None
    anchor: StepPath | None = None
    request: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    status: ControlStatus = "pending"
    error: str | None = None
    created_at: str = ""
    finished_at: str | None = None

    def __post_init__(self) -> None:
        if self.kind in {"start", "rerun", "retry"}:
            if self.message is not None or self.reason is not None:
                raise ValueError("run preparation control cannot carry a message")
            return
        if any(
            value is not None
            for value in (self.input, self.bindings, self.limits, self.resources)
        ):
            raise ValueError("steer and stop controls cannot carry run preparation")
        if self.kind == "steer":
            if self.message is None or self.reason is not None:
                raise ValueError("steer control requires only a message")
        elif self.message is not None:
            raise ValueError("stop control cannot carry a message")


@dataclass(frozen=True, slots=True)
class ThreadControlRecord:
    """One durable mutation applied to a thread."""

    thread: str
    index: int
    kind: ThreadControlKind
    source: str | None = None
    anchor: str | None = None
    request: str | None = None
    expected_head: ThreadControlRef | None = None
    context: dict[str, Any] = field(default_factory=dict)
    status: ControlStatus = "pending"
    created_at: str = ""
    finished_at: str | None = None


def value_ref_from_data(payload: Mapping[str, Any]) -> ValueRef:
    """Parse one durable value reference."""

    if "control" in payload:
        return RunInputRef.from_data(payload)
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

    if isinstance(item, RunInputRef | StepOutputRef):
        return value_ref_to_data(item)
    return {"message": item.to_data()}


def step_inputs_to_data(items: tuple[StepInput, ...]) -> list[dict[str, Any]]:
    """Return serialized step input items."""

    return [step_input_to_data(item) for item in items]


def model_call_from_data(data: object) -> ModelCall:
    """Parse one normalized model call from durable data."""

    return _MODEL_CALL_ADAPTER.validate_python(data)


def model_call_to_data(call: ModelCall) -> dict[str, Any]:
    """Serialize one normalized model call without protocol-only null fields."""

    return {
        "instructions": call.instructions,
        "messages": [message.to_data() for message in call.messages],
        "tools": [tool.to_data() for tool in call.tools],
        "state": dict(call.state) if call.state is not None else None,
    }


def run_limits_to_data(limits: RunLimits) -> dict[str, Any]:
    """Serialize effective limits for one root run tree."""

    return cast(
        dict[str, Any],
        _RUN_LIMITS_ADAPTER.dump_python(limits, mode="json"),
    )


def run_bindings_from_data(data: object) -> RunBindings:
    """Parse effective run bindings from durable data."""

    return _RUN_BINDINGS_ADAPTER.validate_python(data)


def run_bindings_to_data(bindings: RunBindings) -> dict[str, Any]:
    """Serialize effective run bindings."""

    return cast(
        dict[str, Any],
        _RUN_BINDINGS_ADAPTER.dump_python(bindings, mode="json"),
    )


def run_limits_from_data(data: object) -> RunLimits:
    """Parse effective run limits from durable data."""

    return _RUN_LIMITS_ADAPTER.validate_python(data)


def execution_error_from_data(data: object) -> ExecutionError:
    """Parse one execution error from protocol-compatible data."""

    if isinstance(data, str):
        return data
    if isinstance(data, Mapping) and set(data) == {"step"}:
        mapping = cast(Mapping[str, object], data)
        step = mapping.get("step")
        if isinstance(step, str):
            return StepErrorRef(step=StepPath.parse(step))
    raise ValueError("invalid execution error")


def execution_error_to_data(error: ExecutionError) -> str | dict[str, str]:
    """Serialize one execution error for a protocol or storage boundary."""

    if isinstance(error, str):
        return error
    return {"step": str(error.step)}


def execution_error_message(
    error: ExecutionError | None,
    steps: Sequence[StepRecord] = (),
) -> str | None:
    """Resolve one execution error to a displayable message when possible."""

    by_path = {step.path: step for step in steps}
    seen: set[StepPath] = set()
    current = error
    while isinstance(current, StepErrorRef):
        if current.step in seen:
            break
        seen.add(current.step)
        step = by_path.get(current.step)
        if step is None:
            break
        current = step.error
    if isinstance(current, StepErrorRef):
        return f"step {current.step} failed"
    return current


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
