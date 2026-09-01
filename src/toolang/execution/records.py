"""Durable execution record types."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, Literal, TypeAlias, cast

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
from toolang.base.types.model import ModelRequest
from toolang.base.types.policy import RunLimits
from toolang.base.types.run import ModelCall, ModelContinuation, ToolCall
from toolang.lang.ast import FlowStmt, flow_stmt_from_data, to_data as ast_to_data
from toolang.lang.input import PromptInvocation, RunnableInputRaw, parse_input
from toolang.lang.types import Array, Struct, Value, validate_type, value_type
from .types import (
    CollectionStepNoted,
    ControlRef,
    ControlKind,
    ControlStatus,
    ControlTiming,
    AgentResources,
    Local,
    ExecutionError,
    LoopStepNoted,
    ModelAccounting,
    ModelCost,
    ModelCostLine,
    ModelPricing,
    ModelReasoningAccounting,
    ModelStepGiven,
    ModelStepNoted,
    ModelTokenCount,
    ModelTokenPrice,
    ModelUsageMeter,
    Occurrence,
    OccurrencePosition,
    IterationOccurrence,
    RunStatus,
    StepKind,
    StepGiven,
    StepNoted,
    StepPath,
    StepStatus,
    ThreadPeerType,
    ToolStepGiven,
    ToolStepNoted,
    Pointer,
    TypedPointer,
    RunCommand,
    local_from_protocol_data,
    validate_occurrence,
    validate_runtime_value,
    valid_run_id,
    valid_thread_id,
    validate_step_given,
    validate_step_noted,
)

_MODEL_REQUEST_ADAPTER = TypeAdapter(ModelRequest)


_MODEL_CALL_ADAPTER = TypeAdapter(ModelCall)
_TOOL_CALL_ADAPTER = TypeAdapter(ToolCall)
_RUN_LIMITS_ADAPTER = TypeAdapter(RunLimits)
# Caller-facing names remain useful even though both references share one table.
RunControlRef = ControlRef
ThreadControlRef = ControlRef


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Durable run truth."""

    id: str
    parent: StepPath | None
    thread: str
    control: ControlRef
    state: ControlRef
    output: Local | None
    occur: Occurrence | None = None
    status: RunStatus = "pending"
    error: ExecutionError | None = None
    ejected_by: ControlRef | None = None
    created_at: str = ""
    started_at: str = ""
    finished_at: str | None = None

    def __post_init__(self) -> None:
        if not valid_run_id(self.id):
            raise ValueError(f"invalid run id: {self.id!r}")
        if not valid_thread_id(self.thread):
            raise ValueError(f"invalid thread id: {self.thread!r}")
        validate_occurrence(self.occur)


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
class RunControlPayload:
    """Resolved preparation snapshot for one new run."""

    resources: AgentResources
    limits: RunLimits
    state: str | None
    runnable: str
    model: str
    locals: tuple[Local, ...]
    model_request: ModelRequest | None = None
    sandbox: str | None = None
    authored_input: RunnableInputRaw | None = None
    authored_commands: tuple[RunCommand, ...] = ()
    authored_session_commands: tuple[RunCommand, ...] = ()
    prompt_invocations: tuple[PromptInvocation, ...] = ()

    def __post_init__(self) -> None:
        _validate_preparation_payload(
            self.state,
            self.runnable,
            self.model,
            self.model_request,
            self.locals,
            self.sandbox,
        )
        _validate_authored_facts(
            self.authored_input,
            self.authored_commands,
            self.authored_session_commands,
            self.prompt_invocations,
        )


@dataclass(frozen=True, slots=True)
class RerunControlPayload:
    """Resolved preparation snapshot for one rerun."""

    resources: AgentResources
    limits: RunLimits
    state: str
    runnable: str
    model: str
    locals: tuple[Local, ...]
    rerun_from: str
    model_request: ModelRequest | None = None
    sandbox: str | None = None
    authored_input: RunnableInputRaw | None = None
    authored_commands: tuple[RunCommand, ...] = ()
    authored_session_commands: tuple[RunCommand, ...] = ()
    prompt_invocations: tuple[PromptInvocation, ...] = ()

    def __post_init__(self) -> None:
        _validate_preparation_payload(
            self.state,
            self.runnable,
            self.model,
            self.model_request,
            self.locals,
            self.sandbox,
        )
        if not self.rerun_from:
            raise ValueError("rerun payload requires rerun_from")
        _validate_authored_facts(
            self.authored_input,
            self.authored_commands,
            self.authored_session_commands,
            self.prompt_invocations,
        )


@dataclass(frozen=True, slots=True)
class RetryControlPayload:
    """Resolved preparation snapshot for one retry."""

    resources: AgentResources
    limits: RunLimits
    state: str | None
    runnable: str
    model: str
    locals: tuple[Local, ...] | None
    retry_from: StepPath | None
    model_request: ModelRequest | None = None
    sandbox: str | None = None
    authored_input: RunnableInputRaw | None = None
    authored_commands: tuple[RunCommand, ...] = ()
    authored_session_commands: tuple[RunCommand, ...] = ()
    prompt_invocations: tuple[PromptInvocation, ...] = ()

    def __post_init__(self) -> None:
        _validate_preparation_payload(
            self.state,
            self.runnable,
            self.model,
            self.model_request,
            self.locals,
            self.sandbox,
        )
        _validate_authored_facts(
            self.authored_input,
            self.authored_commands,
            self.authored_session_commands,
            self.prompt_invocations,
        )


@dataclass(frozen=True, slots=True)
class ReloadControlPayload:
    """One durable Agent State revision adopted by an active root run."""

    state: str

    def __post_init__(self) -> None:
        _validate_state_revision(self.state, label="reload payload State")


@dataclass(frozen=True, slots=True)
class ExecuteControlPayload:
    """One durable same-Run runnable replacement."""

    state: str
    runnable: str
    module: str
    source: Pointer
    locals: tuple[Local, ...]

    def __post_init__(self) -> None:
        _validate_state_revision(self.state, label="execute payload State")
        if not self.runnable or self.runnable != self.runnable.strip():
            raise ValueError("execute payload requires a canonical runnable")
        if not self.module or self.module != self.module.strip():
            raise ValueError("execute payload requires a canonical module")
        if not isinstance(self.source, Pointer):
            raise TypeError("execute payload source requires a Pointer")
        _validate_control_locals(self.locals)
        for local in self.locals:
            if local.type != "Json":
                raise TypeError("execute payload locals must use raw Json values")
            if not isinstance(local.value, TypedPointer):
                raise TypeError("execute payload locals must point to model input")
            expected = self.source.select("input", "input", local.name or "")
            if local.value.pointer != expected:
                raise ValueError(
                    f"execute payload local {local.name} must point to {expected}"
                )


@dataclass(frozen=True, slots=True)
class SteerControlPayload:
    """Values injected at one agic model boundary."""

    locals: tuple[Local, ...]

    def __post_init__(self) -> None:
        _validate_control_locals(self.locals)


@dataclass(frozen=True, slots=True)
class CancelControlPayload:
    """Optional run cancellation reason values."""

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
    fork_at: str


@dataclass(frozen=True, slots=True)
class RewindControlPayload:
    """Rewind one thread with an optimistic head check."""

    rewind_from: str
    rewind_if: int


PreparationControlPayload = (
    RunControlPayload | RerunControlPayload | RetryControlPayload
)
RunScopedControlPayload = (
    PreparationControlPayload
    | ReloadControlPayload
    | ExecuteControlPayload
    | SteerControlPayload
    | CancelControlPayload
)
ThreadControlPayload = CreateControlPayload | ForkControlPayload | RewindControlPayload
ControlPayload = RunScopedControlPayload | ThreadControlPayload
_CONTROL_PAYLOAD_TYPES = {
    "run": RunControlPayload,
    "rerun": RerunControlPayload,
    "retry": RetryControlPayload,
    "reload": ReloadControlPayload,
    "execute": ExecuteControlPayload,
    "steer": SteerControlPayload,
    "cancel": CancelControlPayload,
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


@dataclass(frozen=True, slots=True)
class ThreadRecord:
    """Durable thread metadata."""

    id: str
    origin: str
    peer: ThreadPeer
    created_by: ControlRef
    head: ControlRef
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if not valid_thread_id(self.id):
            raise ValueError(f"invalid thread id: {self.id!r}")


@dataclass(frozen=True, slots=True)
class ModelCallRefs:
    """Content-addressed durable references for one normalized model call."""

    instructions: str
    messages: tuple[str, ...]
    tools: str | None
    output_schema: dict[str, object] | None
    continuation: ModelContinuation | None

    def __post_init__(self) -> None:
        if not isinstance(self.instructions, str) or not self.instructions:
            raise ValueError("stored model instructions require a reference")
        if not all(isinstance(item, str) and item for item in self.messages):
            raise ValueError("stored model messages require references")
        if self.tools is not None and (
            not isinstance(self.tools, str) or not self.tools
        ):
            raise ValueError("stored model tools require a reference or None")
        if self.output_schema is not None and not isinstance(self.output_schema, dict):
            raise TypeError("stored model output schema requires an object or None")
        if self.continuation is not None and not isinstance(self.continuation, dict):
            raise TypeError("stored model continuation requires an object or None")


@dataclass(frozen=True, slots=True)
class StoredModelStepGiven:
    """Compact durable model Step-begin facts."""

    model: str
    call: ModelCallRefs

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("stored model given requires a model identity")
        if not isinstance(self.call, ModelCallRefs):
            raise TypeError("stored model given requires ModelCallRefs")


StoredStepGiven: TypeAlias = FlowStmt | StoredModelStepGiven | ToolStepGiven


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One durable execution step."""

    path: StepPath
    kind: StepKind
    input: tuple[Pointer, ...]
    given: StoredStepGiven
    state: ControlRef
    output: Local | None
    occur: Occurrence | None = None
    noted: StepNoted = None
    status: StepStatus = "running"
    error: ExecutionError | None = None
    ejected_by: ControlRef | None = None
    created_at: str = ""
    started_at: str = ""
    finished_at: str | None = None

    def __post_init__(self) -> None:
        validate_occurrence(self.occur)
        if isinstance(self.given, StoredModelStepGiven):
            if self.kind != "model":
                raise TypeError(f"{self.kind} Step cannot store model given facts")
        elif self.kind == "model":
            raise TypeError("model Step record requires StoredModelStepGiven")
        else:
            validate_step_given(self.kind, self.given)
        validate_step_noted(self.kind, self.noted, self.status)

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
class ControlRecord:
    """One durable control sent to a run or thread."""

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
        expected_scope = (
            "thread" if self.kind in {"create", "fork", "rewind"} else "run"
        )
        valid_target = (
            valid_thread_id(self.target)
            if expected_scope == "thread"
            else valid_run_id(self.target)
        )
        if not valid_target:
            raise ValueError(
                f"invalid {expected_scope} control target: {self.target!r}"
            )
        if self.index < 0:
            raise ValueError("control index must be non-negative")
        expected = _CONTROL_PAYLOAD_TYPES[self.kind]
        if not isinstance(self.payload, expected):
            raise TypeError(f"{self.kind} control has an invalid payload")


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
        if ":" in raw_tag:
            if set(mapping) != {"?"}:
                raise ValueError("stored pointer requires only one typed ? tag")
            return TypedPointer.parse(raw_tag)
        type_name = validate_type(raw_tag)
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
        return {"?": str(value)}
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
    if kind in {"run", "rerun", "retry"}:
        resources_raw = payload.get("resources")
        limits_raw = payload.get("limits")
        if not isinstance(resources_raw, Mapping) or not isinstance(
            limits_raw, Mapping
        ):
            raise ValueError(f"{kind} payload requires resources and limits")
        resources = AgentResources.from_data(cast(Mapping[str, object], resources_raw))
        limits = run_limits_from_data(limits_raw)
        state = _optional_payload_text(payload, "state")
        runnable = _required_payload_text(payload, "runnable")
        model = _required_payload_text(payload, "model")
        if "model_request" not in payload:
            model_request = (
                None
                if model == "none" and model not in resources.models
                else ModelRequest(model)
            )
        else:
            raw_model_request = payload.get("model_request")
            model_request = (
                None
                if raw_model_request is None
                else _MODEL_REQUEST_ADAPTER.validate_python(raw_model_request)
            )
        if model_request is not None and model_request.ref != model:
            raise ValueError("preparation model request must match model")
        sandbox = _optional_payload_text(payload, "sandbox")
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
        authored_input = _authored_input_from_data(payload.get("authored_input"))
        authored_commands = _run_commands_from_data(
            payload.get("authored_commands", ()),
            label="authored_commands",
        )
        authored_session_commands = _run_commands_from_data(
            payload.get("authored_session_commands", ()),
            label="authored_session_commands",
        )
        prompt_invocations = _prompt_invocations_from_data(
            payload.get("prompt_invocations", ())
        )
        if kind == "run":
            return RunControlPayload(
                resources=resources,
                limits=limits,
                state=state,
                runnable=runnable,
                model=model,
                locals=locals_value or (),
                model_request=model_request,
                sandbox=sandbox,
                authored_input=authored_input,
                authored_commands=authored_commands,
                authored_session_commands=authored_session_commands,
                prompt_invocations=prompt_invocations,
            )
        if kind == "rerun":
            if state is None:
                raise ValueError("rerun payload requires state")
            return RerunControlPayload(
                resources=resources,
                limits=limits,
                state=state,
                runnable=runnable,
                model=model,
                locals=locals_value or (),
                rerun_from=_required_payload_text(payload, "rerun_from"),
                model_request=model_request,
                sandbox=sandbox,
                authored_input=authored_input,
                authored_commands=authored_commands,
                authored_session_commands=authored_session_commands,
                prompt_invocations=prompt_invocations,
            )
        raw_retry_from = payload.get("retry_from")
        return RetryControlPayload(
            resources=resources,
            limits=limits,
            state=state,
            runnable=runnable,
            model=model,
            locals=locals_value,
            retry_from=(
                StepPath.parse(str(raw_retry_from))
                if raw_retry_from is not None
                else None
            ),
            model_request=model_request,
            sandbox=sandbox,
            authored_input=authored_input,
            authored_commands=authored_commands,
            authored_session_commands=authored_session_commands,
            prompt_invocations=prompt_invocations,
        )
    if kind == "reload":
        return ReloadControlPayload(
            state=_required_payload_text(payload, "state"),
        )
    if kind == "execute":
        raw_locals = payload.get("locals")
        if not isinstance(raw_locals, Sequence) or isinstance(
            raw_locals, (str, bytes, bytearray)
        ):
            raise ValueError("execute payload locals must be an array")
        if not all(isinstance(item, Mapping) for item in raw_locals):
            raise ValueError("execute payload contains an invalid local")
        return ExecuteControlPayload(
            state=_required_payload_text(payload, "state"),
            runnable=_required_payload_text(payload, "runnable"),
            module=_required_payload_text(payload, "module"),
            source=Pointer(_required_payload_text(payload, "source")),
            locals=tuple(
                local_decoder(cast(Mapping[str, object], item)) for item in raw_locals
            ),
        )
    if kind in {"steer", "cancel"}:
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
            else CancelControlPayload(locals_value)
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

    if isinstance(payload, RunControlPayload):
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
    if isinstance(payload, ReloadControlPayload):
        return {"state": payload.state}
    if isinstance(payload, ExecuteControlPayload):
        return {
            "state": payload.state,
            "runnable": payload.runnable,
            "module": payload.module,
            "source": str(payload.source),
            "locals": [local_to_data(local) for local in payload.locals],
        }
    if isinstance(payload, SteerControlPayload | CancelControlPayload):
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


def occurrence_from_data(data: object) -> Occurrence | None:
    """Parse one typed runtime occurrence."""

    if data is None:
        return None
    payload = _canonical_object(
        data,
        fields={"item", "lane", "iteration"},
        label="occurrence",
    )
    return Occurrence(
        item=_occurrence_position_from_data(payload["item"], label="item"),
        lane=_occurrence_position_from_data(payload["lane"], label="lane"),
        iteration=_iteration_occurrence_from_data(payload["iteration"]),
    )


def occurrence_to_data(occurrence: Occurrence | None) -> dict[str, object] | None:
    """Serialize one typed runtime occurrence."""

    validate_occurrence(occurrence)
    if occurrence is None:
        return None
    return {
        "item": _occurrence_position_to_data(occurrence.item),
        "lane": _occurrence_position_to_data(occurrence.lane),
        "iteration": (
            {
                "index": occurrence.iteration.index,
                "count": occurrence.iteration.count,
                "phase": occurrence.iteration.phase,
            }
            if occurrence.iteration is not None
            else None
        ),
    }


def _occurrence_position_from_data(
    data: object,
    *,
    label: str,
) -> OccurrencePosition | None:
    if data is None:
        return None
    payload = _canonical_object(
        data,
        fields={"index", "count"},
        label=f"{label} occurrence",
    )
    return OccurrencePosition(
        index=_required_int(payload["index"], label=f"{label} index"),
        count=_required_int(payload["count"], label=f"{label} count"),
    )


def _occurrence_position_to_data(
    occurrence: OccurrencePosition | None,
) -> dict[str, int] | None:
    return (
        {"index": occurrence.index, "count": occurrence.count}
        if occurrence is not None
        else None
    )


def _iteration_occurrence_from_data(data: object) -> IterationOccurrence | None:
    if data is None:
        return None
    payload = _canonical_object(
        data,
        fields={"index", "count", "phase"},
        label="iteration occurrence",
    )
    phase = payload["phase"]
    if phase not in {"body", "until"}:
        raise ValueError("iteration phase must be body or until")
    raw_count = payload["count"]
    return IterationOccurrence(
        index=_required_int(payload["index"], label="iteration index"),
        count=(
            _required_int(raw_count, label="iteration count")
            if raw_count is not None
            else None
        ),
        phase=cast(Literal["body", "until"], phase),
    )


def step_given_from_data(kind: StepKind, data: object) -> StepGiven:
    """Parse one typed Step-begin fact payload from durable data."""

    if kind == "model":
        payload = _canonical_object(data, fields={"model", "call"}, label="model given")
        model = payload["model"]
        if not isinstance(model, str):
            raise ValueError("model given identity must be text")
        return ModelStepGiven(model=model, call=model_call_from_data(payload["call"]))
    if kind == "tool":
        if not isinstance(data, Mapping) or set(data) not in {
            frozenset({"plugin", "call"}),
            frozenset({"plugin", "call", "summary"}),
        }:
            raise ValueError("tool given requires: plugin, call, and optional summary")
        payload = cast(Mapping[str, object], data)
        plugin = payload["plugin"]
        if not isinstance(plugin, str):
            raise ValueError("tool given plugin must be text")
        raw_summary = payload.get("summary", "")
        if not isinstance(raw_summary, str):
            raise ValueError("tool given summary must be text")
        return ToolStepGiven(
            plugin=plugin,
            call=_TOOL_CALL_ADAPTER.validate_python(payload["call"]),
            summary=raw_summary,
        )
    statement = flow_stmt_from_data(data)
    from .types import validate_step_given

    return validate_step_given(kind, statement)


def step_given_to_data(kind: StepKind, given: StepGiven) -> dict[str, object]:
    """Serialize one typed Step-begin fact payload."""

    from .types import validate_step_given

    validate_step_given(kind, given)
    if isinstance(given, ModelStepGiven):
        return {"model": given.model, "call": model_call_to_data(given.call)}
    if isinstance(given, ToolStepGiven):
        data: dict[str, object] = {
            "plugin": given.plugin,
            "call": cast(
                dict[str, object],
                _TOOL_CALL_ADAPTER.dump_python(given.call, mode="json"),
            ),
        }
        if given.summary:
            data["summary"] = given.summary
        return data
    return cast(dict[str, object], ast_to_data(given))


def stored_step_given_from_data(kind: StepKind, data: object) -> StoredStepGiven:
    """Parse the private compact Step-begin representation."""

    if kind != "model":
        return cast(StoredStepGiven, step_given_from_data(kind, data))
    payload = _canonical_object(data, fields={"model", "call"}, label="model given")
    model = payload["model"]
    if not isinstance(model, str) or not model:
        raise ValueError("stored model identity must be text")
    raw_call = payload["call"]
    allowed_call_fields = {
        frozenset({"cont", "instructions", "messages", "tools"}),
        frozenset({"cont", "instructions", "messages", "structured_output", "tools"}),
        frozenset({"cont", "instructions", "messages", "output_schema", "tools"}),
    }
    if not isinstance(raw_call, Mapping) or set(raw_call) not in allowed_call_fields:
        raise ValueError(
            "stored model call requires: cont, instructions, messages, tools, "
            "and optional output_schema"
        )
    call = cast(Mapping[str, object], raw_call)
    instructions = call["instructions"]
    raw_messages = call["messages"]
    raw_tools = call["tools"]
    raw_output_schema = call.get("output_schema", call.get("structured_output"))
    raw_cont = call["cont"]
    if not isinstance(instructions, str) or not instructions:
        raise ValueError("stored model instructions require a reference")
    if (
        not isinstance(raw_messages, Sequence)
        or isinstance(raw_messages, (str, bytes, bytearray))
        or not all(isinstance(item, str) and item for item in raw_messages)
    ):
        raise ValueError("stored model messages require references")
    if raw_tools is not None and not isinstance(raw_tools, str):
        raise ValueError("stored model tools must be a reference or null")
    if raw_output_schema is not None and not isinstance(raw_output_schema, Mapping):
        raise ValueError("stored model output schema must be an object or null")
    if raw_cont is not None and not isinstance(raw_cont, Mapping):
        raise ValueError("stored model cont must be an object or null")
    return StoredModelStepGiven(
        model=model,
        call=ModelCallRefs(
            instructions=instructions,
            messages=tuple(cast(Sequence[str], raw_messages)),
            tools=raw_tools,
            output_schema=(
                dict(cast(Mapping[str, object], raw_output_schema))
                if isinstance(raw_output_schema, Mapping)
                else None
            ),
            continuation=(
                dict(cast(Mapping[str, Any], raw_cont))
                if isinstance(raw_cont, Mapping)
                else None
            ),
        ),
    )


def stored_step_given_to_data(
    kind: StepKind,
    given: StoredStepGiven,
) -> dict[str, object]:
    """Serialize the private compact Step-begin representation."""

    if isinstance(given, StoredModelStepGiven):
        if kind != "model":
            raise TypeError(f"{kind} Step cannot store model given facts")
        return {
            "model": given.model,
            "call": {
                "instructions": given.call.instructions,
                "messages": list(given.call.messages),
                "tools": given.call.tools,
                "output_schema": (
                    dict(given.call.output_schema)
                    if given.call.output_schema is not None
                    else None
                ),
                "cont": (
                    dict(given.call.continuation)
                    if given.call.continuation is not None
                    else None
                ),
            },
        }
    return step_given_to_data(kind, cast(StepGiven, given))


def step_noted_from_data(kind: StepKind, data: object) -> StepNoted:
    """Parse one typed Step-end fact payload from durable data."""

    if data is None:
        return None
    if kind == "tool":
        payload = _canonical_object(data, fields={"summary"}, label="tool noted")
        summary = payload["summary"]
        if not isinstance(summary, str):
            raise ValueError("tool noted summary must be text")
        return ToolStepNoted(summary=summary)
    if kind in {"value", "par"}:
        payload = _canonical_object(
            data,
            fields={"total_items", "output_items"},
            label="collection noted",
        )
        return CollectionStepNoted(
            total_items=_required_int(
                payload["total_items"],
                label="collection total items",
            ),
            output_items=_optional_int(
                payload["output_items"],
                label="collection output items",
            ),
        )
    if kind == "loop":
        if not isinstance(data, Mapping) or set(data) not in {
            frozenset({"iterations", "termination"}),
            frozenset({"iterations", "termination", "total"}),
        }:
            raise ValueError(
                "loop noted requires exactly: iterations, termination, total"
            )
        payload = cast(Mapping[str, object], data)
        termination = payload["termination"]
        if termination not in {"exhausted", "satisfied", "failed", "canceled"}:
            raise ValueError("loop noted termination is invalid")
        return LoopStepNoted(
            iterations=_required_int(payload["iterations"], label="loop iterations"),
            termination=cast(
                Literal["exhausted", "satisfied", "failed", "canceled"],
                termination,
            ),
            total=_optional_int(payload.get("total"), label="loop total"),
        )
    if kind != "model":
        raise ValueError(f"{kind} Step noted must be null")
    if not isinstance(data, Mapping) or set(data) not in {
        frozenset({"tokens", "price", "cost", "cont"}),
        frozenset({"tokens", "price", "cost", "accounting", "cont"}),
    }:
        raise ValueError(
            "model noted requires exactly: accounting, cont, cost, price, tokens"
        )
    legacy = "accounting" not in data
    payload = dict(cast(Mapping[str, object], data))
    payload.setdefault("accounting", None)
    raw_tokens = payload["tokens"]
    tokens = None
    if raw_tokens is not None:
        token_data = _canonical_object(
            raw_tokens,
            fields={"input", "output"},
            label="model tokens",
        )
        tokens = ModelTokenCount(
            input=_required_int(token_data["input"], label="input tokens"),
            output=_required_int(token_data["output"], label="output tokens"),
        )
    raw_price = payload["price"]
    price = None
    if raw_price is not None:
        price_data = _canonical_object(
            raw_price,
            fields={"input", "output"},
            label="model price",
        )
        price = ModelTokenPrice(
            input=_optional_text(price_data["input"], label="input price"),
            output=_optional_text(price_data["output"], label="output price"),
        )
    raw_cont = payload["cont"]
    if raw_cont is not None and not isinstance(raw_cont, Mapping):
        raise ValueError("model noted cont must be an object or null")
    cost = _optional_text(payload["cost"], label="model cost")
    accounting = _model_accounting_from_data(payload["accounting"])
    if legacy:
        accounting = _legacy_model_accounting(tokens=tokens, cost=cost)
    return ModelStepNoted(
        tokens=tokens,
        price=price,
        cost=cost,
        accounting=accounting,
        continuation=(
            dict(cast(Mapping[str, Any], raw_cont))
            if isinstance(raw_cont, Mapping)
            else None
        ),
    )


def _legacy_model_accounting(
    *,
    tokens: ModelTokenCount | None,
    cost: str | None,
) -> ModelAccounting:
    estimate = (
        ModelCost(amount=cost, currency="USD", complete=False)
        if cost is not None
        else None
    )
    return ModelAccounting(
        input_tokens=tokens.input if tokens is not None else 0,
        output_tokens=tokens.output if tokens is not None else 0,
        estimate=estimate,
        selected="estimated" if estimate is not None else "none",
        version=0,
    )


def step_noted_to_data(kind: StepKind, noted: StepNoted) -> dict[str, object] | None:
    """Serialize one typed Step-end fact payload."""

    from .types import validate_step_noted

    validate_step_noted(kind, noted)
    if noted is None:
        return None
    if isinstance(noted, ToolStepNoted):
        return {"summary": noted.summary}
    if isinstance(noted, CollectionStepNoted):
        return {
            "total_items": noted.total_items,
            "output_items": noted.output_items,
        }
    if isinstance(noted, LoopStepNoted):
        return {
            "iterations": noted.iterations,
            "termination": noted.termination,
            "total": noted.total,
        }
    return {
        "tokens": (
            {"input": noted.tokens.input, "output": noted.tokens.output}
            if noted.tokens is not None
            else None
        ),
        "price": (
            {"input": noted.price.input, "output": noted.price.output}
            if noted.price is not None
            else None
        ),
        "cost": noted.cost,
        "accounting": _model_accounting_to_data(noted.accounting),
        "cont": (dict(noted.continuation) if noted.continuation is not None else None),
    }


def _model_accounting_from_data(value: object) -> ModelAccounting | None:
    if value is None:
        return None
    payload = _canonical_object(
        value,
        fields={"version", "usage", "reasoning", "pricing", "cost"},
        label="model accounting",
    )
    version = _required_int(payload["version"], label="model accounting version")
    usage = _canonical_object(
        payload["usage"],
        fields={"input", "output", "meters"},
        label="model accounting usage",
    )
    raw_meters = usage["meters"]
    if not isinstance(raw_meters, list):
        raise ValueError("model accounting meters must be an array")
    meters: list[ModelUsageMeter] = []
    for raw_meter in raw_meters:
        meter = _canonical_object(
            raw_meter,
            fields={"name", "quantity", "unit"},
            label="model accounting meter",
        )
        meters.append(
            ModelUsageMeter(
                name=_required_text(meter["name"], label="model meter name"),
                quantity=_required_text(
                    meter["quantity"], label="model meter quantity"
                ),
                unit=_required_text(meter["unit"], label="model meter unit"),
            )
        )
    reasoning_data = _canonical_object(
        payload["reasoning"],
        fields={"requested", "selected", "reported"},
        label="model accounting reasoning",
    )
    reasoning = ModelReasoningAccounting(
        requested=_optional_dict(
            reasoning_data["requested"], label="requested reasoning"
        ),
        selected=_optional_dict(reasoning_data["selected"], label="selected reasoning"),
        reported=_optional_dict(reasoning_data["reported"], label="reported reasoning"),
    )
    pricing = None
    if payload["pricing"] is not None:
        pricing_data = _canonical_object(
            payload["pricing"],
            fields={"source", "revision", "plan", "match"},
            label="model accounting pricing",
        )
        pricing = ModelPricing(
            source=_required_text(pricing_data["source"], label="pricing source"),
            revision=_optional_text(pricing_data["revision"], label="pricing revision"),
            plan=_required_text(pricing_data["plan"], label="pricing plan"),
            match=_required_dict(pricing_data["match"], label="pricing match"),
        )
    cost_data = _canonical_object(
        payload["cost"],
        fields={"selected", "reported", "estimate"},
        label="model accounting cost",
    )
    selected = _required_text(cost_data["selected"], label="selected cost source")
    if selected not in {"reported", "estimated", "none"}:
        raise ValueError("selected cost source is invalid")
    return ModelAccounting(
        input_tokens=_required_int(usage["input"], label="accounting input tokens"),
        output_tokens=_required_int(usage["output"], label="accounting output tokens"),
        meters=tuple(meters),
        reasoning=reasoning,
        pricing=pricing,
        reported=_model_cost_from_data(cost_data["reported"]),
        estimate=_model_cost_from_data(cost_data["estimate"]),
        selected=cast(Literal["reported", "estimated", "none"], selected),
        version=version,
    )


def _model_cost_from_data(value: object) -> ModelCost | None:
    if value is None:
        return None
    payload = _canonical_object(
        value,
        fields={"amount", "currency", "complete", "lines"},
        label="model cost",
    )
    raw_lines = payload["lines"]
    if not isinstance(raw_lines, list):
        raise ValueError("model cost lines must be an array")
    lines: list[ModelCostLine] = []
    for raw_line in raw_lines:
        line = _canonical_object(
            raw_line,
            fields={"meter", "quantity", "unit", "rate", "per", "amount", "condition"},
            label="model cost line",
        )
        lines.append(
            ModelCostLine(
                meter=_required_text(line["meter"], label="cost line meter"),
                quantity=_required_text(line["quantity"], label="cost line quantity"),
                unit=_required_text(line["unit"], label="cost line unit"),
                rate=_required_text(line["rate"], label="cost line rate"),
                per=_required_text(line["per"], label="cost line per"),
                amount=_required_text(line["amount"], label="cost line amount"),
                condition=_optional_dict(
                    line["condition"], label="cost line condition"
                ),
            )
        )
    complete = payload["complete"]
    if not isinstance(complete, bool):
        raise ValueError("model cost complete must be a boolean")
    return ModelCost(
        amount=_required_text(payload["amount"], label="model cost amount"),
        currency=_required_text(payload["currency"], label="model cost currency"),
        complete=complete,
        lines=tuple(lines),
    )


def _model_accounting_to_data(value: ModelAccounting | None) -> object:
    if value is None:
        return None
    return {
        "version": value.version,
        "usage": {
            "input": value.input_tokens,
            "output": value.output_tokens,
            "meters": [
                {"name": meter.name, "quantity": meter.quantity, "unit": meter.unit}
                for meter in value.meters
            ],
        },
        "reasoning": {
            "requested": value.reasoning.requested,
            "selected": value.reasoning.selected,
            "reported": value.reasoning.reported,
        },
        "pricing": (
            {
                "source": value.pricing.source,
                "revision": value.pricing.revision,
                "plan": value.pricing.plan,
                "match": dict(value.pricing.match),
            }
            if value.pricing is not None
            else None
        ),
        "cost": {
            "selected": value.selected,
            "reported": _model_cost_to_data(value.reported),
            "estimate": _model_cost_to_data(value.estimate),
        },
    }


def _model_cost_to_data(value: ModelCost | None) -> object:
    if value is None:
        return None
    return {
        "amount": value.amount,
        "currency": value.currency,
        "complete": value.complete,
        "lines": [
            {
                "meter": line.meter,
                "quantity": line.quantity,
                "unit": line.unit,
                "rate": line.rate,
                "per": line.per,
                "amount": line.amount,
                "condition": line.condition,
            }
            for line in value.lines
        ],
    }


def _canonical_object(
    data: object,
    *,
    fields: set[str],
    label: str,
) -> Mapping[str, object]:
    if not isinstance(data, Mapping) or set(data) != fields:
        joined = ", ".join(sorted(fields))
        raise ValueError(f"{label} requires exactly: {joined}")
    return cast(Mapping[str, object], data)


def _required_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _optional_int(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    return _required_int(value, label=label)


def _optional_text(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text or null")
    return value


def _required_text(value: object, *, label: str) -> str:
    result = _optional_text(value, label=label)
    if result is None or not result:
        raise ValueError(f"{label} must be non-empty text")
    return result


def _optional_dict(value: object, *, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return _required_dict(value, label=label)


def _required_dict(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return {str(key): item for key, item in value.items()}


def model_call_from_data(data: object) -> ModelCall:
    """Parse one normalized model call from durable data."""

    if isinstance(data, Mapping):
        payload = dict(data)
        if "output_schema" not in payload and "structured_output" in payload:
            payload["output_schema"] = payload["structured_output"]
        payload.pop("structured_output", None)
        data = payload
    return _MODEL_CALL_ADAPTER.validate_python(data)


def model_call_to_data(call: ModelCall) -> dict[str, Any]:
    """Serialize one normalized model call without protocol-only null fields."""

    return {
        "instructions": call.instructions,
        "messages": [message.to_data() for message in call.messages],
        "tools": [tool.to_data() for tool in call.tools],
        "output_schema": (
            dict(call.output_schema) if call.output_schema is not None else None
        ),
        "cont": (dict(call.continuation) if call.continuation is not None else None),
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

    by_pointer = {Pointer.step(step.path, "error"): step for step in steps}
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


def _optional_payload_text(payload: Mapping[str, object], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"control payload requires canonical {name}")
    return value


def _preparation_payload_data(
    payload: RunControlPayload | RerunControlPayload | RetryControlPayload,
) -> dict[str, object]:
    data: dict[str, object] = {
        "resources": payload.resources.to_data(),
        "limits": run_limits_to_data(payload.limits),
        "runnable": payload.runnable,
        "model": payload.model,
        "model_request": (
            _MODEL_REQUEST_ADAPTER.dump_python(payload.model_request, mode="json")
            if payload.model_request is not None
            else None
        ),
        "locals": (
            [local_to_data(local) for local in payload.locals]
            if payload.locals is not None
            else None
        ),
    }
    if payload.state is not None:
        data["state"] = payload.state
    if payload.sandbox is not None:
        data["sandbox"] = payload.sandbox
    if payload.authored_input is not None:
        data["authored_input"] = {
            "primary": payload.authored_input._,
            "named": [
                {"name": item.name, "source": item.source}
                for item in payload.authored_input.named
            ],
        }
    if payload.authored_commands:
        data["authored_commands"] = [
            _run_command_to_data(command) for command in payload.authored_commands
        ]
    if payload.authored_session_commands:
        data["authored_session_commands"] = [
            _run_command_to_data(command)
            for command in payload.authored_session_commands
        ]
    if payload.prompt_invocations:
        data["prompt_invocations"] = [
            _prompt_invocation_to_data(invocation)
            for invocation in payload.prompt_invocations
        ]
    return data


def _authored_input_from_data(value: object) -> RunnableInputRaw | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("preparation authored_input must be an object or null")
    payload = cast(Mapping[str, object], value)
    primary = payload.get("primary")
    if primary is not None and not isinstance(primary, str):
        raise ValueError("preparation authored input primary must be text or null")
    raw_named = payload.get("named", ())
    if not isinstance(raw_named, Sequence) or isinstance(
        raw_named, (str, bytes, bytearray)
    ):
        raise ValueError("preparation authored input named sources must be an array")
    named: list[tuple[str, str]] = []
    for item in raw_named:
        if isinstance(item, Mapping):
            source_item = cast(Mapping[str, object], item)
            name = source_item.get("name")
            source = source_item.get("source")
        elif (
            isinstance(item, Sequence)
            and not isinstance(item, (str, bytes, bytearray))
            and len(item) == 2
        ):
            name, source = item
        else:
            raise ValueError("preparation authored input contains an invalid source")
        if not isinstance(name, str) or not isinstance(source, str):
            raise ValueError("preparation authored input sources require text fields")
        named.append((name, source))
    return parse_input(primary, named=tuple(named))


def _run_commands_from_data(value: object, *, label: str) -> tuple[RunCommand, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"preparation {label} must be an array")
    result: list[RunCommand] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"preparation {label} contains an invalid command")
        command = cast(Mapping[str, object], item)
        group = command.get("group")
        field = command.get("field")
        raw = command.get("value")
        if group == "allow" and isinstance(raw, list):
            raw = tuple(raw)
        elif group == "limit" and field == "cost" and isinstance(raw, str):
            raw = Decimal(raw)
        result.append(RunCommand(cast(Any, group), cast(Any, field), cast(Any, raw)))
    return tuple(result)


def _run_command_to_data(command: RunCommand) -> dict[str, object]:
    value = command.value
    if isinstance(value, tuple):
        encoded: object = list(value)
    elif isinstance(value, Decimal):
        encoded = str(value)
    else:
        encoded = value
    return {"group": command.group, "field": command.field, "value": encoded}


def _prompt_invocations_from_data(value: object) -> tuple[PromptInvocation, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("preparation prompt_invocations must be an array")
    result: list[PromptInvocation] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("preparation contains an invalid prompt invocation")
        invocation = cast(Mapping[str, object], item)
        raw_arguments = invocation.get("arguments", ())
        if not isinstance(raw_arguments, Sequence) or isinstance(
            raw_arguments, (str, bytes, bytearray)
        ):
            raise ValueError("prompt invocation arguments must be an array")
        arguments: list[tuple[str, str]] = []
        for argument in raw_arguments:
            if isinstance(argument, Mapping):
                argument_item = cast(Mapping[str, object], argument)
                name = argument_item.get("name")
                argument_value = argument_item.get("value")
            elif (
                isinstance(argument, Sequence)
                and not isinstance(argument, (str, bytes, bytearray))
                and len(argument) == 2
            ):
                name, argument_value = argument
            else:
                raise ValueError("prompt invocation contains an invalid argument")
            if not isinstance(name, str) or not isinstance(argument_value, str):
                raise ValueError("prompt invocation arguments require text fields")
            arguments.append((name, argument_value))
        parent = invocation.get("parent")
        if parent is not None and (
            isinstance(parent, bool) or not isinstance(parent, int)
        ):
            raise ValueError("prompt invocation parent must be an integer or null")
        result.append(
            PromptInvocation(
                name=str(invocation.get("name", "")),
                arguments=tuple(arguments),
                parent=parent,
                cap_ref=str(invocation.get("cap_ref", "")),
                content_hash=str(invocation.get("content_hash", "")),
            )
        )
    return tuple(result)


def _prompt_invocation_to_data(invocation: PromptInvocation) -> dict[str, object]:
    return {
        "name": invocation.name,
        "arguments": [
            {"name": name, "value": value} for name, value in invocation.arguments
        ],
        "parent": invocation.parent,
        "cap_ref": invocation.cap_ref,
        "content_hash": invocation.content_hash,
    }


def _validate_preparation_payload(
    state: str | None,
    runnable: str,
    model: str,
    model_request: ModelRequest | None,
    locals: tuple[Local, ...] | None,
    sandbox: str | None,
) -> None:
    if state is not None:
        _validate_state_revision(state, label="preparation payload State")
    if not runnable:
        raise ValueError("preparation payload requires runnable")
    if not model:
        raise ValueError("preparation payload requires model")
    if model_request is not None and not isinstance(model_request, ModelRequest):
        raise TypeError("preparation model request must be ModelRequest or none")
    if model_request is not None and model_request.ref != model:
        raise ValueError("preparation model request must match model")
    if sandbox is not None and (
        not isinstance(sandbox, str) or not sandbox or sandbox != sandbox.strip()
    ):
        raise ValueError("preparation payload requires a canonical sandbox")
    if locals is not None:
        _validate_control_locals(locals)


def _validate_authored_facts(
    input: RunnableInputRaw | None,
    commands: tuple[RunCommand, ...],
    session_commands: tuple[RunCommand, ...],
    invocations: tuple[PromptInvocation, ...],
) -> None:
    if input is not None and not isinstance(input, RunnableInputRaw):
        raise TypeError("preparation authored input must be RunnableInputRaw or none")
    for label, values in (
        ("commands", commands),
        ("session commands", session_commands),
    ):
        if not isinstance(values, tuple) or not all(
            isinstance(command, RunCommand) for command in values
        ):
            raise TypeError(f"preparation authored {label} must be RunCommand values")
    if not isinstance(invocations, tuple) or not all(
        isinstance(invocation, PromptInvocation) for invocation in invocations
    ):
        raise TypeError(
            "preparation prompt invocations must be PromptInvocation values"
        )
    for index, invocation in enumerate(invocations):
        if invocation.parent is not None and invocation.parent >= index:
            raise ValueError("prompt invocation parent must precede its child")


def _validate_state_revision(state: object, *, label: str) -> str:
    if (
        not isinstance(state, str)
        or len(state) != 64
        or any(char not in "0123456789abcdef" for char in state)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 revision")
    return state


def _validate_control_locals(locals: tuple[Local, ...]) -> None:
    if not all(isinstance(local, Local) for local in locals):
        raise TypeError("control locals must contain Local values")
    names = tuple(local.name for local in locals)
    if any(name is None for name in names):
        raise ValueError("control locals must be named")
    if len(names) != len(set(names)):
        raise ValueError("control local names must be unique")
