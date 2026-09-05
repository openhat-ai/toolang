"""Shared execution vocabulary and scalar types."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
import math
import re
from typing import Annotated, Any, Literal, TypeAlias, cast

from pydantic import BeforeValidator, Field, WrapSerializer
from pydantic_core import core_schema

from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    part_from_data,
)
from toolang.base.types.model import ModelOverride, ModelRequest
from toolang.base.types.policy import AgentCeiling, RunLimits
from toolang.base.types.run import ModelCall, ModelContinuation, ToolCall
from toolang.lang.ast import (
    AskStmt,
    DropStmt,
    FlowStmt,
    GatherStmt,
    KeepStmt,
    LetStmt,
    MapStmt,
    Node,
    RankStmt,
    RepeatStmt,
    RunStmt,
    ScatterStmt,
    SeekStmt,
    SettleStmt,
    StormStmt,
    flow_stmt_from_data,
    to_data as ast_to_data,
)
from toolang.lang.types import Array, Struct, Value, validate_type, value_type


_EXECUTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_LOCAL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ALLOW_FIELDS = frozenset(
    {
        "models",
        "tools",
        "psyches",
        "skills",
        "services",
        "prompts",
    }
)
DEFAULT_COMMAND_FIELDS = frozenset({"model", "runnable"})
LIMIT_FIELDS = frozenset(
    {"agic_model_calls", "agic_tool_calls", "tokens", "cost", "time"}
)
AllowField = Literal["models", "tools", "psyches", "skills", "services", "prompts"]
LimitField = Literal["agic_model_calls", "agic_tool_calls", "tokens", "cost", "time"]
AllowValue: TypeAlias = tuple[str, ...] | None
LimitValue: TypeAlias = int | Decimal | None
PolicyGroup = Literal["allow", "default", "limit"]
PolicyValue: TypeAlias = tuple[str, ...] | str | int | Decimal | None


@dataclass(frozen=True, slots=True)
class RunCommand:
    """One low-level command retained by execution and restart protocols."""

    group: PolicyGroup
    field: str
    value: PolicyValue

    def __post_init__(self) -> None:
        fields = {
            "allow": ALLOW_FIELDS,
            "default": DEFAULT_COMMAND_FIELDS,
            "limit": LIMIT_FIELDS,
        }.get(self.group)
        if fields is None:
            raise ValueError(f"unknown run command group: {self.group}")
        if not self.field or self.field != self.field.strip():
            raise ValueError("run command requires a canonical field")
        if self.field not in fields:
            raise ValueError(f"unknown {self.group} field: {self.field}")
        if self.group == "allow":
            if self.value is not None and not (
                isinstance(self.value, tuple)
                and all(isinstance(item, str) for item in self.value)
            ):
                raise TypeError("allow command value must be queries, all, or none")
            return
        if self.group == "default":
            if self.value is not None and not isinstance(self.value, str):
                raise TypeError("default command value must be a string or none")
            return
        if self.field == "cost":
            if self.value is not None and not isinstance(self.value, Decimal):
                raise TypeError("limit cost command value must be a decimal or none")
        elif self.value is not None and (
            isinstance(self.value, bool) or not isinstance(self.value, int)
        ):
            raise TypeError(
                f"limit {self.field} command value must be an integer or none"
            )


@dataclass(frozen=True, slots=True)
class AllowOverride:
    """One sparse allow-field replacement."""

    field: AllowField
    value: AllowValue

    def __post_init__(self) -> None:
        if self.field not in ALLOW_FIELDS:
            raise ValueError(f"unknown allow field: {self.field}")
        if self.value is not None and not (
            isinstance(self.value, tuple)
            and all(isinstance(item, str) for item in self.value)
        ):
            raise TypeError("allow override must be queries, all, or none")


@dataclass(frozen=True, slots=True)
class LimitOverride:
    """One sparse run-limit replacement."""

    field: LimitField
    value: LimitValue

    def __post_init__(self) -> None:
        if self.field not in LIMIT_FIELDS:
            raise ValueError(f"unknown run limit: {self.field}")
        if self.field == "cost":
            if self.value is not None and not isinstance(self.value, Decimal):
                raise TypeError("run limit cost override must be a decimal or none")
            if isinstance(self.value, Decimal) and (
                not self.value.is_finite() or self.value < 0
            ):
                raise ValueError(
                    "run limit cost override must be finite and non-negative"
                )
            return
        if self.value is not None and (
            isinstance(self.value, bool) or not isinstance(self.value, int)
        ):
            raise TypeError(
                f"run limit {self.field} override must be an integer or none"
            )
        if isinstance(self.value, int) and self.value < 0:
            raise ValueError(f"run limit {self.field} override must be non-negative")


@dataclass(frozen=True, slots=True)
class SessionSetting:
    """Concrete defaults for subsequent runs in one Chat session."""

    model: ModelRequest | None
    runnable: str | None
    allow: AgentCeiling = AgentCeiling()
    limits: RunLimits = RunLimits()

    def __post_init__(self) -> None:
        if self.model is not None and not isinstance(self.model, ModelRequest):
            raise TypeError("session model must be a ModelRequest or none")
        if self.runnable is not None:
            if not isinstance(self.runnable, str):
                raise TypeError("session runnable must be a string or none")
            if not self.runnable or self.runnable != self.runnable.strip():
                raise ValueError("session runnable must be canonical")
        if not isinstance(self.allow, AgentCeiling):
            raise TypeError("session allow must be an AgentCeiling")
        if not isinstance(self.limits, RunLimits):
            raise TypeError("session limits must be RunLimits")


@dataclass(frozen=True, slots=True)
class RunOverride:
    """Sparse changes attached to exactly one runnable input."""

    model: ModelOverride | None = None
    runnable: str | None = None
    allow: tuple[AllowOverride, ...] = ()
    limits: tuple[LimitOverride, ...] = ()

    def __post_init__(self) -> None:
        if self.model is not None and not isinstance(self.model, ModelOverride):
            raise TypeError("run model override must be a ModelOverride or none")
        if self.runnable is not None:
            if not isinstance(self.runnable, str):
                raise TypeError("run runnable override must be a string or none")
            if not self.runnable or self.runnable != self.runnable.strip():
                raise ValueError("run runnable override must be canonical")
        if not isinstance(self.allow, tuple) or not all(
            isinstance(item, AllowOverride) for item in self.allow
        ):
            raise TypeError("run allow overrides must be AllowOverride objects")
        allow_fields = [item.field for item in self.allow]
        if len(allow_fields) != len(set(allow_fields)):
            raise ValueError("run allow override fields must be unique")
        if not isinstance(self.limits, tuple) or not all(
            isinstance(item, LimitOverride) for item in self.limits
        ):
            raise TypeError("run limit overrides must be LimitOverride objects")
        fields = [item.field for item in self.limits]
        if len(fields) != len(set(fields)):
            raise ValueError("run limit override fields must be unique")

    @property
    def empty(self) -> bool:
        """Return whether no run field was authored."""

        return (
            self.model is None
            and self.runnable is None
            and not self.allow
            and not self.limits
        )


@dataclass(frozen=True, slots=True)
class AgentToolResource:
    """Stable identity of one model-facing tool available to an agent."""

    model_name: str
    plugin: str
    toolset: str
    name: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.model_name, self.plugin, self.toolset, self.name)
        ):
            raise ValueError("agent tool resource fields must be non-empty text")


@dataclass(frozen=True, slots=True)
class AgentCapResource:
    """Stable identity of one State capability available to an agent."""

    kind: str
    name: str
    ref: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.kind, self.name, self.ref)
        ):
            raise ValueError("agent cap resource fields must be non-empty text")


@dataclass(frozen=True, slots=True)
class AgentResources:
    """Stable ordered resources available at one execution point."""

    models: tuple[str, ...] = ()
    tools: tuple[AgentToolResource, ...] = ()
    caps: tuple[AgentCapResource, ...] = ()

    def __post_init__(self) -> None:
        if not all(isinstance(item, str) and item for item in self.models):
            raise ValueError("agent resource models must be non-empty queries")
        if not all(isinstance(item, AgentToolResource) for item in self.tools):
            raise TypeError("agent resource tools must be AgentToolResource objects")
        if not all(isinstance(item, AgentCapResource) for item in self.caps):
            raise TypeError("agent resource caps must be AgentCapResource objects")
        if len(self.models) != len(set(self.models)):
            raise ValueError("agent resource models must be unique")
        tool_names = tuple(item.model_name for item in self.tools)
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("agent resource tools must be unique")
        cap_ids = tuple((item.kind, item.name, item.ref) for item in self.caps)
        if len(cap_ids) != len(set(cap_ids)):
            raise ValueError("agent resource caps must be unique")

    @classmethod
    def from_data(cls, payload: Mapping[str, object]) -> AgentResources:
        """Decode one durable resource snapshot."""

        if set(payload) != {"models", "tools", "caps"}:
            raise ValueError("agent resources require models, tools, and caps")
        raw_models = _resource_array(payload.get("models"), label="models")
        raw_tools = _resource_array(payload.get("tools"), label="tools")
        raw_caps = _resource_array(payload.get("caps"), label="caps")
        if not all(isinstance(item, str) for item in raw_models):
            raise ValueError("agent resources models must contain text")
        models = cast(tuple[str, ...], tuple(raw_models))
        tools = tuple(
            AgentToolResource(**_resource_object(item, kind="tool"))
            for item in raw_tools
        )
        caps = tuple(
            AgentCapResource(**_resource_object(item, kind="cap")) for item in raw_caps
        )
        return cls(models=models, tools=tools, caps=caps)

    def to_data(self) -> dict[str, object]:
        """Encode one resource snapshot using stable public identities."""

        return {
            "models": list(self.models),
            "tools": [
                {
                    "model_name": item.model_name,
                    "plugin": item.plugin,
                    "toolset": item.toolset,
                    "name": item.name,
                }
                for item in self.tools
            ],
            "caps": [
                {"kind": item.kind, "name": item.name, "ref": item.ref}
                for item in self.caps
            ],
        }


def _resource_array(value: object, *, label: str) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"agent resources {label} must be an array")
    return tuple(value)


def _resource_object(
    value: object,
    *,
    kind: Literal["tool", "cap"],
) -> dict[str, str]:
    fields = (
        {"model_name", "plugin", "toolset", "name"}
        if kind == "tool"
        else {"kind", "name", "ref"}
    )
    legacy_tool_fields = {"model_name", "plugin", "namespace", "name"}
    if not isinstance(value, Mapping):
        raise ValueError(f"agent resources {kind} must contain canonical fields")
    if not all(isinstance(item, str) for item in value.values()):
        raise ValueError(f"agent resources {kind} fields must be text")
    data = dict(cast(Mapping[str, object], value))
    if kind == "tool" and set(data) == legacy_tool_fields:
        data["toolset"] = data.pop("namespace")
    if set(data) != fields:
        raise ValueError(f"agent resources {kind} must contain canonical fields")
    return {name: cast(str, data[name]) for name in fields}


@dataclass(frozen=True, slots=True)
class ThreadRef:
    """Reference one durable Thread record."""

    id: str

    def __post_init__(self) -> None:
        if not valid_thread_id(self.id):
            raise ValueError(f"invalid thread ref: {self.id!r}")

    @classmethod
    def parse(cls, value: ThreadRef | str) -> ThreadRef:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise TypeError("thread ref must be text")
        return cls(value)

    def __str__(self) -> str:
        return self.id

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: Any, _handler: Any
    ) -> core_schema.CoreSchema:
        return _text_ref_schema(cls)


@dataclass(frozen=True, slots=True)
class RunRef:
    """Reference one durable Run record."""

    id: str

    def __post_init__(self) -> None:
        if not valid_run_id(self.id):
            raise ValueError(f"invalid run ref: {self.id!r}")

    @classmethod
    def parse(cls, value: RunRef | str) -> RunRef:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise TypeError("run ref must be text")
        return cls(value)

    def __str__(self) -> str:
        return self.id

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: Any, _handler: Any
    ) -> core_schema.CoreSchema:
        return _text_ref_schema(cls)


@dataclass(frozen=True, slots=True)
class StepPath:
    """Identify one Step relative to its owning Run."""

    indices: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.indices or any(
            isinstance(index, bool) or not isinstance(index, int) or index < 0
            for index in self.indices
        ):
            raise ValueError("step path requires non-negative indices")

    @classmethod
    def parse(cls, value: StepPath | tuple[int, ...] | str) -> StepPath:
        if isinstance(value, cls):
            return value
        if isinstance(value, tuple):
            return cls(value)
        if not isinstance(value, str):
            raise TypeError("step path must be text")
        raw_indices = value.split(".")
        if any(not _canonical_index(item) for item in raw_indices):
            raise ValueError(f"invalid step path: {value!r}")
        return cls(tuple(int(item) for item in raw_indices))

    @property
    def parent(self) -> StepPath | None:
        if len(self.indices) == 1:
            return None
        return StepPath(self.indices[:-1])

    @property
    def index(self) -> int:
        return self.indices[-1]

    def child(self, index: int) -> StepPath:
        return StepPath((*self.indices, index))

    def __str__(self) -> str:
        return ".".join(str(index) for index in self.indices)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: Any, _handler: Any
    ) -> core_schema.CoreSchema:
        return _text_ref_schema(cls)


@dataclass(frozen=True, slots=True)
class StepRef:
    """Reference one durable Step record."""

    run: RunRef
    path: StepPath

    def __post_init__(self) -> None:
        if not isinstance(self.run, RunRef):
            raise TypeError("step ref requires a RunRef")
        if not isinstance(self.path, StepPath):
            raise TypeError("step ref requires a StepPath")

    @classmethod
    def parse(cls, value: StepRef | str) -> StepRef:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise TypeError("step ref must be text")
        run, separator, path = value.partition(".")
        if not separator:
            raise ValueError(f"invalid step ref: {value!r}")
        return cls(RunRef(run), StepPath.parse(path))

    @classmethod
    def from_local(
        cls,
        run: RunRef | str,
        path: StepPath | tuple[int, ...] | str,
    ) -> StepRef:
        return cls(RunRef.parse(run), StepPath.parse(path))

    @property
    def run_id(self) -> str:
        return str(self.run)

    @property
    def local(self) -> str:
        return str(self.path)

    @property
    def indices(self) -> tuple[int, ...]:
        return self.path.indices

    @property
    def parent(self) -> StepRef | None:
        parent = self.path.parent
        return StepRef(self.run, parent) if parent is not None else None

    @property
    def index(self) -> int:
        return self.path.index

    def child(self, index: int) -> StepRef:
        return StepRef(self.run, self.path.child(index))

    def __str__(self) -> str:
        return f"{self.run}.{self.path}"

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: Any, _handler: Any
    ) -> core_schema.CoreSchema:
        return _text_ref_schema(cls)


@dataclass(frozen=True, slots=True)
class ControlRef:
    """Reference one durable Control record."""

    target: ThreadRef | RunRef
    index: int

    def __post_init__(self) -> None:
        if not isinstance(self.target, ThreadRef | RunRef):
            raise TypeError("control ref requires a ThreadRef or RunRef")
        if isinstance(self.index, bool) or not isinstance(self.index, int):
            raise TypeError("control index must be an integer")
        if self.index < 0:
            raise ValueError("control index must be non-negative")

    @classmethod
    def parse(cls, value: ControlRef | str) -> ControlRef:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise TypeError("control ref must be text")
        target, separator, raw_index = value.partition("@")
        if not separator or "@" in raw_index or not _canonical_index(raw_index):
            raise ValueError(f"invalid control ref: {value!r}")
        target_ref: ThreadRef | RunRef
        target_ref = RunRef(target) if target.startswith("run_") else ThreadRef(target)
        return cls(target_ref, int(raw_index))

    @classmethod
    def for_run(cls, target: RunRef | str, index: int) -> ControlRef:
        """Build a Run-scoped control reference at a string boundary."""

        return cls(RunRef.parse(target), index)

    @classmethod
    def for_thread(cls, target: ThreadRef | str, index: int) -> ControlRef:
        """Build a Thread-scoped control reference at a string boundary."""

        return cls(ThreadRef.parse(target), index)

    def __str__(self) -> str:
        return f"{self.target}@{self.index}"

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: Any, _handler: Any
    ) -> core_schema.CoreSchema:
        return _text_ref_schema(cls)


_CONTENT_REF_RE = re.compile(r"^sha256_[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ContentRef:
    """Reference one content-addressed blob."""

    id: str

    def __post_init__(self) -> None:
        if _CONTENT_REF_RE.fullmatch(self.id) is None:
            raise ValueError(f"invalid content ref: {self.id!r}")

    @classmethod
    def parse(cls, value: ContentRef | str) -> ContentRef:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise TypeError("content ref must be text")
        return cls(value)

    def __str__(self) -> str:
        return self.id

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: Any, _handler: Any
    ) -> core_schema.CoreSchema:
        return _text_ref_schema(cls)


RecordRef: TypeAlias = ThreadRef | RunRef | StepRef | ControlRef | ContentRef
_RECORD_REF_TYPES = (ThreadRef, RunRef, StepRef, ControlRef, ContentRef)


@dataclass(frozen=True, slots=True)
class JsonPointer:
    """One non-empty RFC 6901 JSON Pointer."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.startswith("/"):
            raise ValueError("JSON pointer must begin with '/'")
        _validate_json_pointer(self.value, source=self.value)

    @classmethod
    def parse(cls, value: JsonPointer | str) -> JsonPointer:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise TypeError("JSON pointer must be text")
        return cls(value)

    @property
    def tokens(self) -> tuple[str, ...]:
        return tuple(
            token.replace("~1", "/").replace("~0", "~")
            for token in self.value[1:].split("/")
        )

    def select(self, *path: str | int) -> JsonPointer:
        if not path:
            return self
        return JsonPointer(f"{self.value}/{_pointer_suffix(path)}")

    def __str__(self) -> str:
        return self.value

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: Any, _handler: Any
    ) -> core_schema.CoreSchema:
        return _text_ref_schema(cls)


@dataclass(frozen=True, slots=True)
class FieldRef:
    """Reference one JSON field within a durable record."""

    record: RecordRef
    field: JsonPointer

    def __post_init__(self) -> None:
        if not isinstance(self.record, _RECORD_REF_TYPES):
            raise TypeError("field ref requires a RecordRef")
        if not isinstance(self.field, JsonPointer):
            raise TypeError("field ref requires a JsonPointer")

    @classmethod
    def parse(cls, value: FieldRef | str) -> FieldRef:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise TypeError("field ref must be text")
        record, separator, field = value.partition("/")
        if not separator:
            raise ValueError(f"invalid field ref: {value!r}")
        return cls(_parse_record_ref(record), JsonPointer(f"/{field}"))

    @classmethod
    def from_path(
        cls,
        record: RecordRef,
        *path: str | int,
    ) -> FieldRef:
        """Build a field reference from decoded record and field components."""

        if not path:
            raise ValueError("field ref requires a non-empty path")
        return cls(record, JsonPointer(f"/{_pointer_suffix(path)}"))

    @property
    def tokens(self) -> tuple[str, ...]:
        return self.field.tokens

    def select(self, *path: str | int) -> FieldRef:
        if not path:
            return self
        return FieldRef(self.record, self.field.select(*path))

    def __str__(self) -> str:
        return f"{self.record}{self.field}"

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: Any, _handler: Any
    ) -> core_schema.CoreSchema:
        return _text_ref_schema(cls)


@dataclass(frozen=True, slots=True)
class TypedRef:
    """Reference one field together with its expected runtime type."""

    ref: FieldRef
    type: str

    def __post_init__(self) -> None:
        if not isinstance(self.ref, FieldRef):
            raise TypeError("typed ref requires a FieldRef")
        validate_type(self.type)

    @classmethod
    def parse(cls, value: TypedRef | str) -> TypedRef:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str) or value.count(":") != 1:
            raise ValueError(f"invalid typed ref: {value!r}")
        ref, type_name = value.rsplit(":", 1)
        return cls(FieldRef.parse(ref), type_name)

    def __str__(self) -> str:
        return f"{self.ref}:{self.type}"

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: Any, _handler: Any
    ) -> core_schema.CoreSchema:
        return _text_ref_schema(cls)


PointerValue: TypeAlias = RecordRef | FieldRef | TypedRef
_POINTER_VALUE_TYPES = (*_RECORD_REF_TYPES, FieldRef, TypedRef)


class PointerType(StrEnum):
    """Concrete Ref variant wrapped by a Pointer."""

    THREAD_REF = "ThreadRef"
    RUN_REF = "RunRef"
    STEP_REF = "StepRef"
    CONTROL_REF = "ControlRef"
    CONTENT_REF = "ContentRef"
    FIELD_REF = "FieldRef"
    TYPED_REF = "TypedRef"


@dataclass(frozen=True, slots=True)
class Pointer:
    """Wrap one parsed durable reference of an otherwise unknown kind."""

    _ref: PointerValue

    def __post_init__(self) -> None:
        if not isinstance(self._ref, _POINTER_VALUE_TYPES):
            raise TypeError("pointer requires a Ref")

    @classmethod
    def parse(cls, value: Pointer | str) -> Pointer:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise TypeError("pointer must be text")
        if ":" in value:
            return cls(TypedRef.parse(value))
        if "/" in value:
            return cls(FieldRef.parse(value))
        return cls(_parse_record_ref(value))

    @property
    def type(self) -> PointerType:
        return PointerType(type(self._ref).__name__)

    def ref(self) -> PointerValue:
        return self._ref

    def thread_ref(self) -> ThreadRef | None:
        return self._ref if isinstance(self._ref, ThreadRef) else None

    def run_ref(self) -> RunRef | None:
        return self._ref if isinstance(self._ref, RunRef) else None

    def step_ref(self) -> StepRef | None:
        return self._ref if isinstance(self._ref, StepRef) else None

    def control_ref(self) -> ControlRef | None:
        return self._ref if isinstance(self._ref, ControlRef) else None

    def content_ref(self) -> ContentRef | None:
        return self._ref if isinstance(self._ref, ContentRef) else None

    def field_ref(self) -> FieldRef | None:
        return self._ref if isinstance(self._ref, FieldRef) else None

    def typed_ref(self) -> TypedRef | None:
        return self._ref if isinstance(self._ref, TypedRef) else None

    def record_ref(self) -> RecordRef:
        ref = self._ref
        if isinstance(ref, TypedRef):
            return ref.ref.record
        if isinstance(ref, FieldRef):
            return ref.record
        return ref

    @property
    def kind(self) -> Literal["thread", "control", "run", "step", "content"]:
        ref = self.record_ref()
        if isinstance(ref, ThreadRef):
            return "thread"
        if isinstance(ref, ControlRef):
            return "control"
        if isinstance(ref, RunRef):
            return "run"
        if isinstance(ref, StepRef):
            return "step"
        return "content"

    @property
    def tokens(self) -> tuple[str, ...]:
        ref = self._ref
        if isinstance(ref, TypedRef):
            return ref.ref.tokens
        if isinstance(ref, FieldRef):
            return ref.tokens
        return ()

    def select(self, *path: str | int) -> Pointer:
        if not path:
            return self
        ref = self._ref
        if isinstance(ref, TypedRef):
            raise ValueError("cannot select below a TypedRef")
        if isinstance(ref, FieldRef):
            return Pointer(ref.select(*path))
        return Pointer(FieldRef(ref, JsonPointer(f"/{_pointer_suffix(path)}")))

    def __str__(self) -> str:
        return str(self._ref)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: Any, _handler: Any
    ) -> core_schema.CoreSchema:
        return _text_ref_schema(cls)


_PART_TYPES = (
    TextPart,
    ImagePart,
    AudioPart,
    DocumentPart,
    ToolCallPart,
    ToolResultPart,
)
_PART_TYPES_BY_NAME = {
    "Part": _PART_TYPES,
    "TextPart": TextPart,
    "ImagePart": ImagePart,
    "AudioPart": AudioPart,
    "DocumentPart": DocumentPart,
    "ToolCallPart": ToolCallPart,
    "ToolResultPart": ToolResultPart,
}
_PART_PROTOCOL_TYPES = frozenset(
    {"text", "image", "audio", "document", "tool_call", "tool_result"}
)


@dataclass(frozen=True, slots=True)
class Local:
    """One runtime value and its local-table binding semantics."""

    value: Value | TypedRef
    name: str | None = None
    dim: Literal[0, 1] = 0

    @classmethod
    def typed(
        cls,
        type_name: str,
        value: object,
        name: str | None = None,
        dim: Literal[0, 1] = 0,
    ) -> Local:
        """Build a local by applying one explicit typed boundary."""

        return cls(
            value=value_for_type(type_name, value),
            name=name,
            dim=dim,
        )

    def __post_init__(self) -> None:
        if self.name is not None and not _LOCAL_NAME_RE.fullmatch(self.name):
            raise ValueError(f"invalid local name: {self.name!r}")
        if self.dim not in {0, 1}:
            raise ValueError(f"unsupported local dimension: {self.dim!r}")
        if not isinstance(self.value, TypedRef):
            object.__setattr__(
                self,
                "value",
                value_for_type(value_type(self.value), self.value),
            )
        validate_runtime_value(self.value, self.type)
        if self.dim == 1 and not self.type.endswith("[]"):
            raise ValueError("dim=1 requires an array value type")
        if self.dim == 1 and not isinstance(self.value, Array | TypedRef):
            raise TypeError("dim=1 requires an array value or whole-value pointer")

    @property
    def type(self) -> str:
        """Return the canonical runtime or expected pointer type."""

        if isinstance(self.value, TypedRef):
            return self.value.type
        return value_type(self.value)

    @property
    def item_type(self) -> str:
        """Return the execution item type for this local."""

        return self.type[:-2] if self.dim == 1 else self.type

    @classmethod
    def _validate_pydantic(cls, value: object) -> Local:
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return local_from_protocol_data(cast(Mapping[str, object], value))
        raise TypeError("local must be a Local or canonical object")

    @staticmethod
    def _serialize_pydantic(value: Local) -> dict[str, object]:
        return local_to_protocol_data(value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: Any,
        _handler: Any,
    ) -> core_schema.CoreSchema:
        protocol_schema = core_schema.typed_dict_schema(
            {
                "type": core_schema.typed_dict_field(core_schema.str_schema()),
                "value": core_schema.typed_dict_field(core_schema.any_schema()),
                "name": core_schema.typed_dict_field(
                    core_schema.nullable_schema(core_schema.str_schema())
                ),
                "dim": core_schema.typed_dict_field(core_schema.literal_schema([0, 1])),
            }
        )
        return core_schema.json_or_python_schema(
            json_schema=core_schema.no_info_after_validator_function(
                cls._validate_pydantic,
                protocol_schema,
            ),
            python_schema=core_schema.union_schema(
                [
                    core_schema.is_instance_schema(cls),
                    core_schema.no_info_plain_validator_function(
                        cls._validate_pydantic
                    ),
                ]
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(
                cls._serialize_pydantic,
                return_schema=core_schema.dict_schema(
                    core_schema.str_schema(), core_schema.any_schema()
                ),
            ),
        )


def local_from_protocol_data(payload: Mapping[str, object]) -> Local:
    """Parse one caller-facing local projection."""

    if set(payload) != {"type", "value", "name", "dim"}:
        raise ValueError("local requires type, value, name, and dim fields")
    raw_type = payload.get("type")
    if not isinstance(raw_type, str):
        raise ValueError("local type must be text")
    type_name = validate_type(raw_type)
    raw_dim = payload.get("dim")
    if isinstance(raw_dim, bool) or not isinstance(raw_dim, int):
        raise ValueError("local dim must be 0 or 1")
    raw_name = payload.get("name")
    if raw_name is not None and not isinstance(raw_name, str):
        raise ValueError("local name must be text or null")
    return Local.typed(
        type_name,
        value_from_protocol_data(payload.get("value"), type_name),
        name=raw_name,
        dim=cast(Literal[0, 1], raw_dim),
    )


def local_to_protocol_data(local: Local) -> dict[str, object]:
    """Serialize one caller-facing local projection."""

    return {
        "type": local.type,
        "value": _protocol_value_to_data(local.value),
        "name": local.name,
        "dim": local.dim,
    }


def value_from_protocol_data(data: object, type_name: str) -> Value | TypedRef:
    """Parse one canonical protocol value at an explicit type boundary."""

    if isinstance(data, Mapping) and set(data) == {"?"}:
        raw_tag = cast(Mapping[str, object], data).get("?")
        if isinstance(raw_tag, str):
            typed = TypedRef.parse(raw_tag)
            if not type_assignable(typed.type, type_name):
                raise TypeError(f"protocol pointer is {typed.type}, not {type_name}")
            return typed
    if type_name.endswith("[]"):
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes, bytearray)):
            raise ValueError(f"protocol {type_name} requires an array")
        return cast(
            Value,
            Array(
                type_name,
                tuple(value_from_protocol_data(item, type_name[:-2]) for item in data),
            ),
        )
    if type_name in _PART_TYPES_BY_NAME:
        if not isinstance(data, Mapping):
            raise ValueError(f"protocol {type_name} requires an object")
        return part_from_data(cast(Mapping[str, Any], data))
    if type_name not in {"Text", "Number", "Boolean", "Json"}:
        if not isinstance(data, Mapping):
            raise ValueError(f"protocol {type_name} requires an object")
        if not all(isinstance(name, str) for name in data):
            raise ValueError("protocol struct fields must be text")
        return cast(
            Value,
            Struct(
                type_name,
                {
                    cast(str, name): _protocol_json_value_from_data(item)
                    for name, item in data.items()
                },
            ),
        )
    return cast(Value, _protocol_json_value_from_data(data))


def _protocol_json_value_from_data(data: object) -> object:
    if isinstance(data, Mapping):
        mapping = cast(Mapping[str, object], data)
        if not all(isinstance(name, str) for name in mapping):
            raise ValueError("protocol object keys must be text")
        if mapping.get("type") in _PART_PROTOCOL_TYPES:
            return part_from_data(cast(Mapping[str, Any], mapping))
        return {
            name: _protocol_json_value_from_data(item) for name, item in mapping.items()
        }
    if isinstance(data, list):
        return tuple(_protocol_json_value_from_data(item) for item in data)
    return data


def _protocol_value_to_data(value: object) -> object:
    if isinstance(value, TypedRef):
        return {"?": str(value)}
    if isinstance(value, _PART_TYPES):
        return value.to_data()
    if isinstance(value, Array):
        return [_protocol_value_to_data(item) for item in value]
    if isinstance(value, Struct | Mapping):
        return {
            str(name): _protocol_value_to_data(item) for name, item in value.items()
        }
    if isinstance(value, tuple | list):
        return [_protocol_value_to_data(item) for item in value]
    return value


def validate_runtime_value(
    value: object, type_name: str, *, path: str = "local"
) -> None:
    """Validate one concrete or referenced execution value against a type."""

    validate_type(type_name)
    if isinstance(value, TypedRef):
        if not type_assignable(value.type, type_name):
            raise TypeError(f"{path} pointer is {value.type}, not {type_name}")
        return
    if type_name.endswith("[]"):
        if not isinstance(value, Array) or value.type != type_name:
            raise TypeError(f"{path} is not {type_name}")
        item_type = type_name[:-2]
        for index, item in enumerate(value):
            validate_runtime_value(item, item_type, path=f"{path}[{index}]")
        return
    expected_part = _PART_TYPES_BY_NAME.get(type_name)
    if expected_part is not None:
        if not isinstance(value, expected_part):
            raise TypeError(f"{path} is not {type_name}")
        return
    if type_name in {"Text", "Number", "Boolean"}:
        valid = value_type(value) == type_name
    elif type_name == "Json":
        _validate_open_local_value(value, path=path)
        return
    else:
        valid = isinstance(value, Struct) and value.type == type_name
        if valid and isinstance(value, Struct):
            _validate_open_local_value(value, path=path)
            return
    if not valid:
        raise TypeError(f"{path} is not {type_name}")


def value_for_type(type_name: str, value: object) -> Value | TypedRef:
    """Normalize one value at an explicit Toolang typed boundary."""

    validate_type(type_name)
    if isinstance(value, TypedRef):
        result: object = value
    elif isinstance(value, FieldRef):
        result = TypedRef(value, type_name)
    elif type_name.endswith("[]"):
        if isinstance(value, Array):
            result = value
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            item_type = type_name[:-2]
            result = Array(
                type_name,
                tuple(value_for_type(item_type, item) for item in value),
            )
        else:
            result = value
    elif type_name == "Json":
        result = _normalize_json_value(value)
    elif type_name not in {
        "Text",
        "Number",
        "Boolean",
        "Json",
        "Part",
        *_PART_TYPES_BY_NAME,
    } and isinstance(value, Mapping):
        if not all(isinstance(name, str) for name in value):
            raise TypeError("struct field names must be strings")
        result = Struct(
            type_name,
            {cast(str, name): item for name, item in value.items()},
        )
    else:
        result = value
    validate_runtime_value(result, type_name)
    return cast(Value | TypedRef, result)


def _normalize_json_value(value: object) -> object:
    if isinstance(value, Struct):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(name, str) for name in value):
            raise TypeError("Json object keys must be strings")
        return {
            cast(str, name): _normalize_json_value(item) for name, item in value.items()
        }
    if isinstance(value, tuple | list):
        return tuple(_normalize_json_value(item) for item in value)
    return value


def _validate_open_local_value(value: object, *, path: str) -> None:
    if isinstance(value, (TypedRef, *_PART_TYPES)):
        return
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise TypeError(f"{path} contains a non-finite number")
    if isinstance(value, Array):
        validate_runtime_value(value, value.type, path=path)
        return
    if isinstance(value, tuple | list):
        for index, item in enumerate(value):
            _validate_open_local_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for name, item in value.items():
            if not isinstance(name, str):
                raise TypeError(f"{path} contains a non-text key")
            _validate_open_local_value(item, path=f"{path}.{name}")
        return
    raise TypeError(f"{path} contains unsupported {type(value).__name__}")


def type_assignable(actual: str, expected: str) -> bool:
    """Return whether one runtime type satisfies an expected boundary."""

    validate_type(actual)
    validate_type(expected)
    if expected == "Json" or actual == expected:
        return True
    return expected == "Part" and actual in set(_PART_TYPES_BY_NAME) - {"Part"}


@dataclass(frozen=True, slots=True)
class ErrorMessage:
    """A directly owned execution error message."""

    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("error message must be non-empty text")

    @classmethod
    def from_data(cls, value: object) -> ErrorMessage:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping) or set(value) != {"type", "message"}:
            raise ValueError("error message requires type and message")
        payload = cast(Mapping[str, object], value)
        if payload.get("type") != "message":
            raise ValueError("error message type must be 'message'")
        message = payload.get("message")
        if not isinstance(message, str):
            raise ValueError("error message must be text")
        return cls(message)

    def to_data(self) -> dict[str, str]:
        return {"type": "message", "message": self.message}

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: Any, _handler: Any
    ) -> core_schema.CoreSchema:
        return _tagged_value_schema(cls, cls.from_data, cls.to_data)


@dataclass(frozen=True, slots=True)
class ErrorRef:
    """Reference the Run or Step field that owns an execution error."""

    ref: FieldRef

    def __post_init__(self) -> None:
        if not isinstance(self.ref, FieldRef):
            raise TypeError("error ref requires a FieldRef")
        if not isinstance(self.ref.record, RunRef | StepRef):
            raise ValueError("error ref must reference a Run or Step")
        if self.ref.field.value != "/error":
            raise ValueError("error ref must reference exactly /error")

    @classmethod
    def from_data(cls, value: object) -> ErrorRef:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping) or set(value) != {"type", "ref"}:
            raise ValueError("error ref requires type and ref")
        payload = cast(Mapping[str, object], value)
        if payload.get("type") != "ref":
            raise ValueError("error ref type must be 'ref'")
        raw_ref = payload.get("ref")
        if not isinstance(raw_ref, str):
            raise ValueError("error ref must be text")
        return cls(FieldRef.parse(raw_ref))

    def to_data(self) -> dict[str, str]:
        return {"type": "ref", "ref": str(self.ref)}

    def __str__(self) -> str:
        return str(self.ref)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: Any, _handler: Any
    ) -> core_schema.CoreSchema:
        return _tagged_value_schema(cls, cls.from_data, cls.to_data)


RunStatus = Literal["pending", "running", "succeeded", "failed", "canceled"]
StepStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "failed",
    "canceled",
]
ControlStatus = Literal["pending", "applied", "wontapply", "revoked"]

StepKind = Literal[
    "run",
    "agent",
    "human",
    "model",
    "tool",
    "par",
    "loop",
    "value",
]


@dataclass(frozen=True, slots=True)
class ModelStepGiven:
    """Resolved model identity and normalized call known at Step begin."""

    model: str
    call: ModelCall

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("model Step given requires a model identity")
        if not isinstance(self.call, ModelCall):
            raise TypeError("model Step given requires a ModelCall")


@dataclass(frozen=True, slots=True)
class ToolStepGiven:
    """Resolved plugin identity and model-emitted call known at Step begin."""

    plugin: str
    call: ToolCall
    summary: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.plugin, str) or not self.plugin:
            raise ValueError("tool Step given requires a plugin identity")
        if not isinstance(self.call, ToolCall):
            raise TypeError("tool Step given requires a ToolCall")
        _validate_step_summary(self.summary, label="tool Step given", allow_empty=True)


def _serialize_step_given(value: StepGiven, handler: Any) -> object:
    if isinstance(value, Node):
        return ast_to_data(value)
    return handler(value)


def _parse_step_given(value: object) -> object:
    payload = cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}
    if isinstance(payload.get("kind"), str):
        return flow_stmt_from_data(value)
    return value


StepGiven: TypeAlias = Annotated[
    FlowStmt | ModelStepGiven | ToolStepGiven,
    BeforeValidator(_parse_step_given),
    WrapSerializer(_serialize_step_given),
]


@dataclass(frozen=True, slots=True)
class ModelTokenCount:
    """Input and output tokens consumed by one model Step."""

    input: int
    output: int

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.input, self.output)
        ):
            raise ValueError("model token counts must be non-negative integers")


@dataclass(frozen=True, slots=True)
class ModelTokenPrice:
    """Decimal-text prices applied to one model Step."""

    input: str | None
    output: str | None

    def __post_init__(self) -> None:
        _validate_decimal_text(self.input, label="input token price")
        _validate_decimal_text(self.output, label="output token price")


@dataclass(frozen=True, slots=True)
class ModelUsageMeter:
    """One auditable quantity within model-call accounting."""

    name: str
    quantity: str
    unit: str = "token"

    def __post_init__(self) -> None:
        if not self.name or not self.unit:
            raise ValueError("model usage meter name and unit are required")
        _validate_decimal_text(self.quantity, label="model usage meter quantity")


@dataclass(frozen=True, slots=True)
class ModelCostLine:
    """One applied quantity and rate contributing to a model cost."""

    meter: str
    quantity: str
    unit: str
    rate: str
    per: str
    amount: str
    condition: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.meter or not self.unit:
            raise ValueError("model cost line meter and unit are required")
        for label, value in (
            ("quantity", self.quantity),
            ("rate", self.rate),
            ("per", self.per),
            ("amount", self.amount),
        ):
            _validate_decimal_text(value, label=f"model cost line {label}")
        if self.condition is not None and not isinstance(self.condition, dict):
            raise TypeError("model cost line condition must be an object")


@dataclass(frozen=True, slots=True)
class ModelCost:
    """One provider-reported or catalog-estimated monetary amount."""

    amount: str
    currency: str
    complete: bool
    lines: tuple[ModelCostLine, ...] = ()

    def __post_init__(self) -> None:
        _validate_decimal_text(self.amount, label="model cost amount")
        if not self.currency:
            raise ValueError("model cost currency is required")
        if not isinstance(self.complete, bool):
            raise TypeError("model cost completeness must be a boolean")


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """Catalog pricing provenance captured for one completed call."""

    source: str
    revision: str | None = None
    plan: str = "standard"
    match: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source or not self.plan:
            raise ValueError("model pricing source and plan are required")
        if not isinstance(self.match, dict):
            raise TypeError("model pricing match must be an object")


@dataclass(frozen=True, slots=True)
class ModelReasoningAccounting:
    """Requested, selected, and provider-reported reasoning controls."""

    requested: dict[str, Any] | None = None
    selected: dict[str, Any] | None = None
    reported: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        for value in (self.requested, self.selected, self.reported):
            if value is not None and not isinstance(value, dict):
                raise TypeError("model reasoning accounting values must be objects")


@dataclass(frozen=True, slots=True)
class ModelAccounting:
    """Versioned durable accounting for one completed model call."""

    input_tokens: int
    output_tokens: int
    meters: tuple[ModelUsageMeter, ...] = ()
    reasoning: ModelReasoningAccounting = ModelReasoningAccounting()
    pricing: ModelPricing | None = None
    reported: ModelCost | None = None
    estimate: ModelCost | None = None
    selected: Literal["reported", "estimated", "none"] = "none"
    version: int = 1

    def __post_init__(self) -> None:
        if self.version not in {0, 1}:
            raise ValueError(f"unsupported model accounting version: {self.version}")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.input_tokens, self.output_tokens)
        ):
            raise ValueError("model accounting totals must be non-negative integers")
        if self.selected not in {"reported", "estimated", "none"}:
            raise ValueError("invalid selected model cost source")
        if self.selected == "reported" and self.reported is None:
            raise ValueError("reported model cost selection requires reported cost")
        if self.selected == "estimated" and self.estimate is None:
            raise ValueError("estimated model cost selection requires an estimate")


@dataclass(frozen=True, slots=True)
class ModelStepNoted:
    """Accounting and continuation learned when a model Step ends."""

    tokens: ModelTokenCount | None = None
    price: ModelTokenPrice | None = None
    cost: str | None = None
    accounting: ModelAccounting | None = None
    continuation: ModelContinuation | None = field(
        default=None,
        metadata={
            "validation_alias": "cont",
            "serialization_alias": "cont",
        },
    )

    def __post_init__(self) -> None:
        if self.tokens is not None and not isinstance(self.tokens, ModelTokenCount):
            raise TypeError("model Step tokens require ModelTokenCount")
        if self.price is not None and not isinstance(self.price, ModelTokenPrice):
            raise TypeError("model Step price requires ModelTokenPrice")
        _validate_decimal_text(self.cost, label="model cost")
        if self.accounting is not None and not isinstance(
            self.accounting, ModelAccounting
        ):
            raise TypeError("model Step accounting requires ModelAccounting")
        if self.continuation is not None and not isinstance(self.continuation, dict):
            raise TypeError("model Step continuation must be an object")


@dataclass(frozen=True, slots=True)
class ToolStepNoted:
    """Human-readable terminal summary learned when a tool Step ends."""

    summary: str

    def __post_init__(self) -> None:
        _validate_step_summary(self.summary, label="tool Step noted")


LoopTermination = Literal["exhausted", "satisfied", "failed", "canceled"]


@dataclass(frozen=True, slots=True)
class CollectionStepNoted:
    """Collection cardinality learned while executing one Flow Step."""

    total_items: int
    output_items: int | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("total", self.total_items),
            ("output", self.output_items),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int)
            ):
                raise TypeError(f"collection Step {label} items must be an integer")
            if value is not None and value < 0:
                raise ValueError(f"collection Step {label} items must be non-negative")
        if self.output_items is not None and self.output_items > self.total_items:
            raise ValueError("collection Step output items cannot exceed total items")


@dataclass(frozen=True, slots=True)
class LoopStepNoted:
    """Iteration count and terminal cause learned when a loop Step ends."""

    iterations: int
    termination: LoopTermination
    total: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.iterations, bool) or not isinstance(self.iterations, int):
            raise TypeError("loop Step iterations must be an integer")
        if self.iterations < 0:
            raise ValueError("loop Step iterations must be non-negative")
        if self.total is not None:
            if isinstance(self.total, bool) or not isinstance(self.total, int):
                raise TypeError("loop Step total must be an integer or None")
            if self.total < 0:
                raise ValueError("loop Step total must be non-negative")
            if self.iterations > self.total:
                raise ValueError("loop Step iterations cannot exceed total")
        if self.termination not in {
            "exhausted",
            "satisfied",
            "failed",
            "canceled",
        }:
            raise ValueError(f"unknown loop Step termination: {self.termination}")


StepNoted: TypeAlias = (
    ModelStepNoted | ToolStepNoted | CollectionStepNoted | LoopStepNoted | None
)


@dataclass(frozen=True, slots=True)
class OccurrencePosition:
    """One zero-based position within a known total."""

    index: int
    count: int

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (self.index, self.count)
        ):
            raise TypeError("occurrence positions must be integers")
        if self.index < 0 or self.count < 1 or self.index >= self.count:
            raise ValueError("occurrence position must be within its count")


@dataclass(frozen=True, slots=True)
class IterationOccurrence:
    """One loop iteration and the phase executing within it."""

    index: int
    phase: Literal["body", "until"]
    count: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int):
            raise TypeError("iteration index must be an integer")
        if self.index < 0:
            raise ValueError("iteration index must be non-negative")
        if self.phase not in {"body", "until"}:
            raise ValueError(f"unknown iteration phase: {self.phase}")
        if self.count is not None and (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or self.count < 1
            or self.index >= self.count
        ):
            raise ValueError("iteration count must contain the iteration index")


@dataclass(frozen=True, slots=True)
class Occurrence:
    """Runtime position of one Run or Step within structural execution."""

    item: OccurrencePosition | None = None
    lane: OccurrencePosition | None = None
    iteration: IterationOccurrence | None = None

    def __post_init__(self) -> None:
        if self.item is not None and not isinstance(self.item, OccurrencePosition):
            raise TypeError("item occurrence requires OccurrencePosition")
        if self.lane is not None and not isinstance(self.lane, OccurrencePosition):
            raise TypeError("lane occurrence requires OccurrencePosition")
        if self.iteration is not None and not isinstance(
            self.iteration, IterationOccurrence
        ):
            raise TypeError("iteration occurrence requires IterationOccurrence")
        if self.item is None and self.lane is None and self.iteration is None:
            raise ValueError("occurrence requires item, lane, or iteration")


def validate_occurrence(occurrence: Occurrence | None) -> Occurrence | None:
    """Reject loose runtime occurrence payloads at typed boundaries."""

    if occurrence is not None and not isinstance(occurrence, Occurrence):
        raise TypeError("occurrence requires an Occurrence or None")
    return occurrence


def validate_step_given(kind: StepKind, given: StepGiven) -> StepGiven:
    """Validate one begin payload against its enclosing Step kind."""

    if kind == "model":
        if not isinstance(given, ModelStepGiven):
            raise TypeError("model Step requires ModelStepGiven")
        return given
    if kind == "tool":
        if not isinstance(given, ToolStepGiven):
            raise TypeError("tool Step requires ToolStepGiven")
        return given
    if not _flow_statement_matches_kind(given, kind):
        raise TypeError(f"{kind} Step requires a compatible FlowStmt")
    return given


def validate_step_noted(
    kind: StepKind,
    noted: StepNoted,
    status: StepStatus | None = None,
) -> StepNoted:
    """Validate one end payload against its enclosing Step kind."""

    if kind == "model":
        if noted is not None and not isinstance(noted, ModelStepNoted):
            raise TypeError("model Step noted requires ModelStepNoted or None")
        return noted
    if kind == "tool":
        if noted is not None and not isinstance(noted, ToolStepNoted):
            raise TypeError("tool Step noted requires ToolStepNoted or None")
        if status in {"pending", "running"} and noted is not None:
            raise ValueError(f"{status} tool Step cannot have terminal noted facts")
        return noted
    if kind == "loop":
        if noted is not None and not isinstance(noted, LoopStepNoted):
            raise TypeError("loop Step noted requires LoopStepNoted or None")
        if status in {"pending", "running"} and noted is not None:
            raise ValueError(f"{status} loop Step cannot have terminal noted facts")
        if (
            status == "succeeded"
            and noted is not None
            and noted.termination
            not in {
                "exhausted",
                "satisfied",
            }
        ):
            raise ValueError("succeeded loop Step requires a successful termination")
        if status in {"failed", "canceled"} and noted is not None:
            if noted.termination != status:
                raise ValueError(
                    f"{status} loop Step requires {status} termination facts"
                )
        return noted
    if kind in {"value", "par"}:
        if noted is not None and not isinstance(noted, CollectionStepNoted):
            raise TypeError(f"{kind} Step noted requires CollectionStepNoted or None")
        if status in {"pending", "running"} and noted is not None:
            raise ValueError(f"{status} {kind} Step cannot have terminal noted facts")
        if status == "succeeded" and noted is not None:
            if noted.output_items is None:
                raise ValueError(
                    f"succeeded {kind} Step requires collection output items"
                )
        if status in {"failed", "canceled"} and noted is not None:
            if noted.output_items is not None:
                raise ValueError(
                    f"{status} {kind} Step cannot have collection output items"
                )
        return noted
    if noted is not None:
        raise TypeError(f"{kind} Step does not accept noted facts")
    return None


def _validate_step_summary(
    summary: object,
    *,
    label: str,
    allow_empty: bool = False,
) -> None:
    if not isinstance(summary, str):
        raise TypeError(f"{label} summary must be text")
    if not summary:
        if allow_empty:
            return
        raise ValueError(f"{label} summary must be non-empty")
    if summary != summary.strip() or "\n" in summary or "\r" in summary:
        raise ValueError(f"{label} summary must be canonical single-line text")


def _flow_statement_matches_kind(value: object, kind: StepKind) -> bool:
    if kind == "value":
        return isinstance(value, LetStmt) or (
            isinstance(value, KeepStmt | DropStmt) and value.runnable is None
        )
    if kind == "run":
        return isinstance(value, RunStmt | ScatterStmt | GatherStmt)
    if kind == "agent":
        return isinstance(value, SeekStmt)
    if kind == "human":
        return isinstance(value, AskStmt)
    if kind == "par":
        return isinstance(value, StormStmt | MapStmt | RankStmt) or (
            isinstance(value, KeepStmt | DropStmt) and value.runnable is not None
        )
    if kind == "loop":
        return isinstance(value, SettleStmt | RepeatStmt)
    return False


def _validate_decimal_text(value: str | None, *, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty decimal text")
    try:
        parsed = Decimal(value)
    except Exception as exc:
        raise ValueError(f"{label} must be decimal text") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{label} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class RulesRecallTarget:
    workspace: str
    path: str
    kind: Literal["rules"] = field(default="rules", init=False)


@dataclass(frozen=True, slots=True)
class SkillRecallTarget:
    ref: str
    kind: Literal["skill"] = field(default="skill", init=False)


@dataclass(frozen=True, slots=True)
class ServiceRecallTarget:
    ref: str
    kind: Literal["service"] = field(default="service", init=False)


RecallTarget = Annotated[
    RulesRecallTarget | SkillRecallTarget | ServiceRecallTarget,
    Field(discriminator="kind"),
]
ControlTiming = Literal["immediate", "next_step", "next_call"]
ControlKind = Literal[
    "run",
    "recall",
    "retry",
    "reload",
    "execute",
    "steer",
    "cancel",
    "create",
    "fork",
    "rewind",
]
ThreadPeerType = Literal["user", "agent"]


class ThreadPrefix(StrEnum):
    """Canonical prefixes for locally issued thread ids."""

    SCRIPT = "script"
    WEB = "web"
    TERM = "term"


def valid_execution_id(value: object) -> bool:
    """Return whether a run or thread id has canonical durable syntax."""

    return isinstance(value, str) and _EXECUTION_ID_RE.fullmatch(value) is not None


def valid_run_id(value: object) -> bool:
    """Return whether a run id occupies the reserved run namespace."""

    return valid_execution_id(value) and cast(str, value).startswith("run_")


def valid_thread_id(value: object) -> bool:
    """Return whether a thread id is disjoint from the run namespace."""

    return (
        valid_execution_id(value)
        and not cast(str, value).startswith("run_")
        and not cast(str, value).startswith("sha256_")
    )


def validate_execution_id(value: object, *, label: str) -> str:
    """Return one canonical run or thread id, or reject it before persistence."""

    if not valid_execution_id(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return cast(str, value)


def _canonical_index(value: str) -> bool:
    return (
        bool(value) and value.isascii() and value.isdigit() and str(int(value)) == value
    )


def _parse_record_ref(value: str) -> RecordRef:
    if value.startswith("sha256_"):
        return ContentRef(value)
    if "@" in value:
        return ControlRef.parse(value)
    if value.startswith("run_"):
        return StepRef.parse(value) if "." in value else RunRef(value)
    return ThreadRef(value)


def _text_ref_schema(value_type: type[Any]) -> core_schema.CoreSchema:
    parse = value_type.parse
    return core_schema.json_or_python_schema(
        json_schema=core_schema.no_info_after_validator_function(
            parse,
            core_schema.str_schema(),
        ),
        python_schema=core_schema.union_schema(
            [
                core_schema.is_instance_schema(value_type),
                core_schema.no_info_after_validator_function(
                    parse,
                    core_schema.str_schema(),
                ),
            ]
        ),
        serialization=core_schema.plain_serializer_function_ser_schema(
            str,
            return_schema=core_schema.str_schema(),
        ),
    )


def _tagged_value_schema(
    value_type: type[Any],
    parse: Any,
    serialize: Any,
) -> core_schema.CoreSchema:
    return core_schema.json_or_python_schema(
        json_schema=core_schema.no_info_after_validator_function(
            parse,
            core_schema.dict_schema(),
        ),
        python_schema=core_schema.union_schema(
            [
                core_schema.is_instance_schema(value_type),
                core_schema.no_info_after_validator_function(
                    parse,
                    core_schema.dict_schema(),
                ),
            ]
        ),
        serialization=core_schema.plain_serializer_function_ser_schema(
            serialize,
            return_schema=core_schema.dict_schema(),
        ),
    )


def _validate_json_pointer(value: str, *, source: str) -> None:
    if ":" in value:
        raise ValueError(f"pointer field names cannot contain ':': {source!r}")
    index = 0
    while index < len(value):
        if value[index] != "~":
            index += 1
            continue
        if index + 1 >= len(value) or value[index + 1] not in {"0", "1"}:
            raise ValueError(f"invalid JSON pointer escape: {source!r}")
        index += 2


def _pointer_suffix(path: Sequence[str | int]) -> str:
    return "/".join(str(item).replace("~", "~0").replace("/", "~1") for item in path)
