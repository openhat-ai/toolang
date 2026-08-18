"""Durable execution record types."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, cast

from pydantic import BeforeValidator, TypeAdapter, ValidationInfo

from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    MessageRole,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    part_from_data,
)
from toolang.base.types.policy import RunLimits
from toolang.base.types.run import ModelCall
from toolang.lang.types import Array, Struct, Value, validate_type, value_type
from .types import (
    ControlRef,
    ControlKind,
    ControlStatus,
    ControlTiming,
    AgentResources,
    Local,
    ExecutionError,
    RunId,
    RunStatus,
    StepKind,
    StepPath,
    StepStatus,
    ThreadPeerType,
    Pointer,
    TypedPointer,
    local_from_protocol_data,
    validate_runtime_value,
)


_MODEL_CALL_ADAPTER = TypeAdapter(ModelCall)
_RUN_LIMITS_ADAPTER = TypeAdapter(RunLimits)
# Caller-facing names remain useful even though both references share one table.
RunControlRef = ControlRef
ThreadControlRef = ControlRef


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Durable run truth."""

    id: RunId
    parent: StepPath | None
    thread: str
    control: ControlRef
    output: Local | None
    placement: dict[str, object] | None = None
    status: RunStatus = "pending"
    error: ExecutionError | None = None
    ejected_by: ControlRef | None = None
    created_at: str = ""
    started_at: str = ""
    finished_at: str | None = None


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
class StartControlPayload:
    """Resolved preparation snapshot for one new run."""

    resources: AgentResources
    limits: RunLimits
    runnable: str
    model: str
    locals: tuple[Local, ...]

    def __post_init__(self) -> None:
        _validate_preparation_payload(self.runnable, self.model, self.locals)


@dataclass(frozen=True, slots=True)
class RerunControlPayload:
    """Resolved preparation snapshot for one rerun."""

    resources: AgentResources
    limits: RunLimits
    runnable: str
    model: str
    locals: tuple[Local, ...]
    rerun_from: RunId

    def __post_init__(self) -> None:
        _validate_preparation_payload(self.runnable, self.model, self.locals)
        if not self.rerun_from:
            raise ValueError("rerun payload requires rerun_from")


@dataclass(frozen=True, slots=True)
class RetryControlPayload:
    """Resolved preparation snapshot for one retry."""

    resources: AgentResources
    limits: RunLimits
    runnable: str
    model: str
    locals: tuple[Local, ...] | None
    retry_from: StepPath | None

    def __post_init__(self) -> None:
        _validate_preparation_payload(self.runnable, self.model, self.locals)


@dataclass(frozen=True, slots=True)
class SteerControlPayload:
    """Values injected at one agic model boundary."""

    locals: tuple[Local, ...]

    def __post_init__(self) -> None:
        _validate_control_locals(self.locals)


@dataclass(frozen=True, slots=True)
class StopControlPayload:
    """Optional stop reason values."""

    locals: tuple[Local, ...] = ()

    def __post_init__(self) -> None:
        _validate_control_locals(self.locals)


@dataclass(frozen=True, slots=True)
class CreateControlPayload:
    """Create one empty thread."""


@dataclass(frozen=True, slots=True)
class ForkControlPayload:
    """Fork one thread at a visible run."""

    fork_from: str
    fork_at: RunId


@dataclass(frozen=True, slots=True)
class RewindControlPayload:
    """Rewind one thread with an optimistic head check."""

    rewind_from: RunId
    rewind_if: int


PreparationControlPayload = (
    StartControlPayload | RerunControlPayload | RetryControlPayload
)
RunControlPayload = PreparationControlPayload | SteerControlPayload | StopControlPayload
ThreadControlPayload = CreateControlPayload | ForkControlPayload | RewindControlPayload
ControlPayload = RunControlPayload | ThreadControlPayload
_CONTROL_PAYLOAD_TYPES = {
    "start": StartControlPayload,
    "rerun": RerunControlPayload,
    "retry": RetryControlPayload,
    "steer": SteerControlPayload,
    "stop": StopControlPayload,
    "create": CreateControlPayload,
    "fork": ForkControlPayload,
    "rewind": RewindControlPayload,
}


def _control_payload_variant(value: object, info: ValidationInfo) -> object:
    """Decode a payload using the enclosing record's control kind."""

    kind = info.data.get("kind")
    if kind not in _CONTROL_PAYLOAD_TYPES:
        raise ValueError(f"unknown control kind: {kind}")
    expected = _CONTROL_PAYLOAD_TYPES[kind]
    if isinstance(value, Mapping):
        return control_payload_from_protocol_data(cast(ControlKind, kind), value)
    if not isinstance(value, expected):
        raise ValueError(f"{kind} control has an invalid payload")
    return value


ControlPayloadField = Annotated[
    ControlPayload,
    BeforeValidator(_control_payload_variant),
]
RunControlPayloadField = Annotated[
    RunControlPayload,
    BeforeValidator(_control_payload_variant),
]
ThreadControlPayloadField = Annotated[
    ThreadControlPayload,
    BeforeValidator(_control_payload_variant),
]


@dataclass(frozen=True, slots=True)
class ThreadRecord:
    """Durable thread metadata."""

    thread_id: str
    origin: str
    peer: ThreadPeer
    created_by: ControlRef
    head: ControlRef
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One durable execution step."""

    path: StepPath
    kind: StepKind
    input: tuple[Pointer, ...]
    output: Local | None
    placement: dict[str, object] | None = None
    given: dict[str, Any] = field(default_factory=dict)
    noted: dict[str, Any] = field(default_factory=dict)
    status: StepStatus = "running"
    error: ExecutionError | None = None
    ejected_by: ControlRef | None = None
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
class ControlRecordBase:
    """Fields shared by durable run and thread controls."""

    target: str
    index: int
    kind: ControlKind
    payload: ControlPayloadField
    request: str | None = None
    status: ControlStatus = "pending"
    timing: ControlTiming = "immediate"
    error: str | None = None
    created_at: str = ""
    finished_at: str | None = None

    def __post_init__(self) -> None:
        if not self.target:
            raise ValueError("control target must be non-empty")
        if self.index < 0:
            raise ValueError("control index must be non-negative")
        expected = _CONTROL_PAYLOAD_TYPES[self.kind]
        if not isinstance(self.payload, expected):
            raise TypeError(f"{self.kind} control has an invalid payload")


@dataclass(frozen=True, slots=True)
class RunControlRecord(ControlRecordBase):
    """One durable control sent to a run."""

    kind: Literal["start", "rerun", "retry", "steer", "stop"]
    payload: RunControlPayloadField

    @property
    def run(self) -> RunId:
        return self.target


@dataclass(frozen=True, slots=True)
class ThreadControlRecord(ControlRecordBase):
    """One durable mutation applied to a thread."""

    kind: Literal["create", "fork", "rewind"]
    payload: ThreadControlPayloadField

    @property
    def thread(self) -> str:
        return self.target


_PART_STORAGE_TYPES = {
    "TextPart": "text",
    "ImagePart": "image",
    "AudioPart": "audio",
    "DocumentPart": "document",
    "ToolCallPart": "tool_call",
    "ToolResultPart": "tool_result",
}


def local_from_data(payload: Mapping[str, object]) -> Local:
    """Parse one local from its private durable representation."""

    if set(payload) != {"value", "name", "dim"}:
        raise ValueError("stored local requires value, name, and dim fields")
    raw_dim = payload.get("dim")
    if isinstance(raw_dim, bool) or not isinstance(raw_dim, int):
        raise ValueError("local dim must be 0 or 1")
    dim = cast(Literal[0, 1], raw_dim)
    raw_name = payload.get("name")
    if raw_name is not None and not isinstance(raw_name, str):
        raise ValueError("local name must be text or null")
    name = raw_name
    return Local(
        value=local_value_from_data(payload.get("value")),
        name=name,
        dim=dim,
    )


def local_to_data(local: Local) -> dict[str, object]:
    """Serialize one local using its private durable representation."""

    return {
        "value": local_value_to_data(local.value),
        "name": local.name,
        "dim": local.dim,
    }


def local_value_from_data(data: object) -> Value | TypedPointer:
    """Parse one self-describing stored value."""

    if isinstance(data, Mapping):
        mapping = cast(Mapping[str, object], data)
        if not all(isinstance(name, str) for name in mapping):
            raise ValueError("stored object keys must be text")
        raw_tag = mapping.get("?")
        if not isinstance(raw_tag, str):
            raise ValueError("stored object requires a text ? tag")
        if raw_tag.endswith("!"):
            type_name = validate_type(raw_tag[:-1])
            if set(mapping) != {"?", "!"}:
                raise ValueError("boxed stored value requires only ? and ! fields")
            return _boxed_value_from_data(type_name, mapping.get("!"))
        type_name, separator, raw_pointer = raw_tag.partition("@")
        validate_type(type_name)
        if separator:
            if set(mapping) != {"?"} or not raw_pointer:
                raise ValueError("stored pointer requires only one typed ? tag")
            return TypedPointer(type_name, Pointer(raw_pointer))
        fields = {name: item for name, item in mapping.items() if name != "?"}
        if "!" in fields:
            raise ValueError("inline stored value cannot contain !")
        if type_name in _PART_STORAGE_TYPES:
            discriminator = _PART_STORAGE_TYPES[type_name]
            return part_from_data(
                cast(Mapping[str, Any], {**fields, "type": discriminator})
            )
        decoded = {name: local_value_from_data(item) for name, item in fields.items()}
        if type_name == "Json":
            return cast(Value, decoded)
        if type_name.endswith("[]") or type_name in {
            "Text",
            "Number",
            "Boolean",
            "Part",
        }:
            raise ValueError(f"{type_name} cannot use inline stored fields")
        return cast(Value, Struct(type_name, decoded))
    if isinstance(data, list):
        raise ValueError("stored arrays require a typed boxed value")
    if data is None or isinstance(data, str | bool | int | float):
        if data is None:
            raise ValueError("null requires an explicit stored type")
        return cast(Value, data)
    raise ValueError(f"unsupported stored value: {type(data).__name__}")


def local_value_to_data(value: Value | TypedPointer) -> object:
    """Serialize one self-describing stored value."""

    if isinstance(value, TypedPointer):
        return {"?": f"{value.type}@{value.pointer}"}
    if isinstance(
        value,
        (
            TextPart,
            ImagePart,
            AudioPart,
            DocumentPart,
            ToolCallPart,
            ToolResultPart,
        ),
    ):
        payload = value.to_data()
        payload.pop("type", None)
        return {"?": type(value).__name__, **payload}
    if isinstance(value, Array):
        return {
            "?": f"{value.type}!",
            "!": [
                local_value_to_data(cast(Value | TypedPointer, item)) for item in value
            ],
        }
    if isinstance(value, Struct):
        if {"?", "!"}.intersection(value):
            raise ValueError("? and ! are reserved for stored values")
        return {
            "?": value.type,
            **{
                name: local_value_to_data(cast(Value | TypedPointer, item))
                for name, item in value.items()
            },
        }
    if isinstance(value, Mapping):
        reserved = {"?", "!"}.intersection(value)
        if reserved:
            marker = sorted(reserved)[0]
            raise ValueError(f"{marker} is reserved for execution values")
        return {
            "?": "Json",
            **{
                str(key): local_value_to_data(cast(Value | TypedPointer, item))
                for key, item in value.items()
            },
        }
    if isinstance(value, tuple | list):
        return {
            "?": "Json!",
            "!": [
                local_value_to_data(cast(Value | TypedPointer, item)) for item in value
            ],
        }
    if value is None:
        return {"?": "Json!", "!": None}
    if isinstance(value, str | bool | int | float):
        return value
    raise TypeError(f"unsupported stored value: {type(value).__name__}")


def _boxed_value_from_data(type_name: str, data: object) -> Value | TypedPointer:
    if type_name.endswith("[]"):
        if not isinstance(data, list):
            raise ValueError(f"stored {type_name} requires an array ! value")
        result = Array(
            type_name,
            tuple(local_value_from_data(item) for item in data),
        )
        validate_runtime_value(result, type_name, path="stored value")
        return cast(Value, result)
    if type_name == "Json":
        if isinstance(data, list):
            return cast(Value, tuple(local_value_from_data(item) for item in data))
        if isinstance(data, Mapping):
            raise ValueError("stored Json objects must use inline fields")
        if data is None or isinstance(data, str | bool | int | float):
            return cast(Value, data)
    if type_name in {"Text", "Number", "Boolean"}:
        if value_type(data) == type_name:
            return cast(Value, data)
    raise ValueError(f"invalid boxed {type_name} value")


def control_payload_from_data(
    kind: ControlKind,
    data: object,
) -> ControlPayload:
    """Parse one typed control payload from durable data."""

    return _control_payload_from_data(kind, data, local_from_data)


def control_payload_from_protocol_data(
    kind: ControlKind,
    data: object,
) -> ControlPayload:
    """Parse one typed control payload from its caller-facing projection."""

    return _control_payload_from_data(kind, data, local_from_protocol_data)


def _control_payload_from_data(
    kind: ControlKind,
    data: object,
    local_decoder: Callable[[Mapping[str, object]], Local],
) -> ControlPayload:

    if not isinstance(data, Mapping):
        raise ValueError("control payload must be an object")
    payload = cast(Mapping[str, object], data)
    if kind in {"start", "rerun", "retry"}:
        resources_raw = payload.get("resources")
        limits_raw = payload.get("limits")
        if not isinstance(resources_raw, Mapping) or not isinstance(
            limits_raw, Mapping
        ):
            raise ValueError(f"{kind} payload requires resources and limits")
        resources = AgentResources.from_data(cast(Mapping[str, object], resources_raw))
        limits = run_limits_from_data(limits_raw)
        runnable = _required_payload_text(payload, "runnable")
        model = _required_payload_text(payload, "model")
        raw_locals = payload.get("locals")
        if raw_locals is None:
            locals_value = None
        elif isinstance(raw_locals, Sequence) and not isinstance(
            raw_locals, (str, bytes, bytearray)
        ):
            if not all(isinstance(item, Mapping) for item in raw_locals):
                raise ValueError(f"{kind} payload contains an invalid local")
            locals_value = tuple(
                local_decoder(cast(Mapping[str, object], item)) for item in raw_locals
            )
        else:
            raise ValueError(f"{kind} payload locals must be an array or null")
        if kind != "retry" and locals_value is None:
            raise ValueError(f"{kind} payload requires locals")
        if kind == "start":
            return StartControlPayload(
                resources=resources,
                limits=limits,
                runnable=runnable,
                model=model,
                locals=locals_value or (),
            )
        if kind == "rerun":
            return RerunControlPayload(
                resources=resources,
                limits=limits,
                runnable=runnable,
                model=model,
                locals=locals_value or (),
                rerun_from=_required_payload_text(payload, "rerun_from"),
            )
        raw_retry_from = payload.get("retry_from")
        return RetryControlPayload(
            resources=resources,
            limits=limits,
            runnable=runnable,
            model=model,
            locals=locals_value,
            retry_from=(
                StepPath.parse(str(raw_retry_from))
                if raw_retry_from is not None
                else None
            ),
        )
    if kind in {"steer", "stop"}:
        raw_locals = payload.get("locals", ())
        if not isinstance(raw_locals, Sequence) or isinstance(
            raw_locals, (str, bytes, bytearray)
        ):
            raise ValueError(f"{kind} payload locals must be an array")
        locals_value = tuple(
            local_decoder(cast(Mapping[str, object], item))
            for item in raw_locals
            if isinstance(item, Mapping)
        )
        if len(locals_value) != len(raw_locals):
            raise ValueError(f"{kind} payload contains an invalid local")
        return (
            SteerControlPayload(locals_value)
            if kind == "steer"
            else StopControlPayload(locals_value)
        )
    if kind == "create":
        if payload:
            raise ValueError("create payload must be empty")
        return CreateControlPayload()
    if kind == "fork":
        return ForkControlPayload(
            fork_from=_required_payload_text(payload, "fork_from"),
            fork_at=_required_payload_text(payload, "fork_at"),
        )
    if kind == "rewind":
        raw_rewind_if = payload.get("rewind_if")
        if isinstance(raw_rewind_if, bool) or not isinstance(raw_rewind_if, int):
            raise ValueError("rewind payload requires an integer rewind_if")
        return RewindControlPayload(
            rewind_from=_required_payload_text(payload, "rewind_from"),
            rewind_if=raw_rewind_if,
        )
    raise ValueError(f"unknown control kind: {kind}")


def control_payload_to_data(payload: ControlPayload) -> dict[str, object]:
    """Serialize one typed control payload."""

    if isinstance(payload, StartControlPayload):
        return _preparation_payload_data(payload)
    if isinstance(payload, RerunControlPayload):
        return {**_preparation_payload_data(payload), "rerun_from": payload.rerun_from}
    if isinstance(payload, RetryControlPayload):
        return {
            **_preparation_payload_data(payload),
            "locals": (
                [local_to_data(local) for local in payload.locals]
                if payload.locals is not None
                else None
            ),
            "retry_from": (
                str(payload.retry_from) if payload.retry_from is not None else None
            ),
        }
    if isinstance(payload, SteerControlPayload | StopControlPayload):
        return {"locals": [local_to_data(local) for local in payload.locals]}
    if isinstance(payload, CreateControlPayload):
        return {}
    if isinstance(payload, ForkControlPayload):
        return {"fork_from": payload.fork_from, "fork_at": payload.fork_at}
    return {"rewind_from": payload.rewind_from, "rewind_if": payload.rewind_if}


def pointers_from_data(data: object) -> tuple[Pointer, ...]:
    """Parse a pointer-only record field."""

    if not isinstance(data, Sequence) or isinstance(data, (str, bytes, bytearray)):
        raise ValueError("value pointers must be an array")
    if not all(isinstance(item, str) for item in data):
        raise ValueError("value pointers must contain only strings")
    return tuple(Pointer(cast(str, item)) for item in data)


def pointers_to_data(items: Sequence[Pointer]) -> list[str]:
    """Serialize a pointer-only record field."""

    return [str(item) for item in items]


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


def run_limits_from_data(data: object) -> RunLimits:
    """Parse effective run limits from durable data."""

    return _RUN_LIMITS_ADAPTER.validate_python(data)


def execution_error_from_data(data: object) -> ExecutionError:
    """Parse one execution error from protocol-compatible data."""

    if isinstance(data, str):
        return data
    if isinstance(data, Mapping) and set(data) == {"?"}:
        tag = cast(Mapping[str, object], data).get("?")
        if isinstance(tag, str) and tag.startswith("@") and len(tag) > 1:
            return Pointer(tag[1:])
    raise ValueError("invalid execution error")


def execution_error_to_data(error: ExecutionError) -> str | dict[str, str]:
    """Serialize one execution error for a protocol or storage boundary."""

    if isinstance(error, str):
        return error
    return {"?": f"@{error}"}


def execution_error_message(
    error: ExecutionError | None,
    steps: Sequence[StepRecord] = (),
) -> str | None:
    """Resolve one execution error to a displayable message when possible."""

    by_pointer = {Pointer(_step_pointer(step.path)): step for step in steps}
    seen: set[Pointer] = set()
    current = error
    while isinstance(current, Pointer):
        if current in seen:
            break
        seen.add(current)
        step = by_pointer.get(current)
        if step is None:
            break
        current = step.error
    if isinstance(current, Pointer):
        return f"execution value {current} failed"
    return current


def step_message_role(kind: StepKind) -> MessageRole | None:
    """Return the transcript role produced by one step kind."""

    if kind == "model":
        return "assistant"
    if kind == "tool":
        return "tool"
    return None


def _required_payload_text(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"control payload requires {name}")
    return value


def _preparation_payload_data(
    payload: StartControlPayload | RerunControlPayload | RetryControlPayload,
) -> dict[str, object]:
    return {
        "resources": payload.resources.to_data(),
        "limits": run_limits_to_data(payload.limits),
        "runnable": payload.runnable,
        "model": payload.model,
        "locals": (
            [local_to_data(local) for local in payload.locals]
            if payload.locals is not None
            else None
        ),
    }


def _validate_preparation_payload(
    runnable: str,
    model: str,
    locals: tuple[Local, ...] | None,
) -> None:
    if not runnable:
        raise ValueError("preparation payload requires runnable")
    if not model:
        raise ValueError("preparation payload requires model")
    if locals is not None:
        _validate_control_locals(locals)


def _validate_control_locals(locals: tuple[Local, ...]) -> None:
    if not all(isinstance(local, Local) for local in locals):
        raise TypeError("control locals must contain Local values")
    names = tuple(local.name for local in locals)
    if any(name is None for name in names):
        raise ValueError("control locals must be named")
    if len(names) != len(set(names)):
        raise ValueError("control local names must be unique")


def _step_pointer(path: StepPath) -> str:
    return ".".join((path.run, *(str(index) for index in path.indices)))
