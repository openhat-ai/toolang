"""Shared execution vocabulary and scalar types."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
import math
import re
from typing import Annotated, Any, Literal, TypeAlias, cast

from pydantic import BeforeValidator, PlainSerializer
from pydantic_core import core_schema

from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.lang.types import Array, Struct, Value, validate_type, value_type


RunId = str
_EXECUTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_LOCAL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PolicyGroup = Literal["allow", "default", "limit"]
PolicyValue: TypeAlias = tuple[str, ...] | str | int | Decimal | None
ALLOW_POLICY_FIELDS = frozenset(
    {
        "models",
        "tools",
        "caps",
        "psyches",
        "skills",
        "services",
        "prompts",
    }
)
DEFAULT_POLICY_FIELDS = frozenset({"model", "runnable"})
LIMIT_POLICY_FIELDS = frozenset(
    {"agic_model_calls", "agic_tool_calls", "tokens", "cost", "time"}
)


@dataclass(frozen=True, slots=True)
class RunOverride:
    """One canonical caller command applied to execution policy."""

    group: PolicyGroup
    field: str
    value: PolicyValue

    def __post_init__(self) -> None:
        if self.group not in {"allow", "default", "limit"}:
            raise ValueError(f"unknown policy command group: {self.group}")
        if not self.field or self.field != self.field.strip():
            raise ValueError("policy command requires a canonical field")
        fields = {
            "allow": ALLOW_POLICY_FIELDS,
            "default": DEFAULT_POLICY_FIELDS,
            "limit": LIMIT_POLICY_FIELDS,
        }[self.group]
        if self.field not in fields:
            raise ValueError(f"unknown {self.group} field: {self.field}")
        if self.group == "allow":
            if self.value is not None and not (
                isinstance(self.value, tuple)
                and all(isinstance(item, str) for item in self.value)
            ):
                raise TypeError("allow policy value must be selectors, all, or none")
            return
        if self.group == "default":
            if self.value is not None and not isinstance(self.value, str):
                raise TypeError("default policy value must be a string or none")
            return
        if self.field == "cost":
            if self.value is not None and not isinstance(self.value, Decimal):
                raise TypeError("limit cost policy value must be a decimal or none")
        elif self.value is not None and (
            isinstance(self.value, bool) or not isinstance(self.value, int)
        ):
            raise TypeError(
                f"limit {self.field} policy value must be an integer or none"
            )


@dataclass(frozen=True, slots=True)
class AgentToolResource:
    """Stable identity of one model-facing tool available to an agent."""

    model_name: str
    plugin: str
    namespace: str
    name: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.model_name, self.plugin, self.namespace, self.name)
        ):
            raise ValueError("agent tool resource fields must be non-empty text")


@dataclass(frozen=True, slots=True)
class AgentCapResource:
    """Stable identity of one prepared cap available to an agent."""

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
            raise ValueError("agent resource models must be non-empty selectors")
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
                    "namespace": item.namespace,
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
        {"model_name", "plugin", "namespace", "name"}
        if kind == "tool"
        else {"kind", "name", "ref"}
    )
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"agent resources {kind} must contain canonical fields")
    if not all(isinstance(item, str) for item in value.values()):
        raise ValueError(f"agent resources {kind} fields must be text")
    data = cast(Mapping[str, object], value)
    return {name: cast(str, data[name]) for name in fields}


@dataclass(frozen=True, slots=True)
class Pointer:
    """Reference one immutable control, step, or run value."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("value pointer must be non-empty text")
        anchor, separator, pointer = self.value.partition("/")
        if not anchor or any(character.isspace() for character in anchor):
            raise ValueError(f"invalid value pointer: {self.value!r}")
        if separator:
            if pointer.endswith("/"):
                raise ValueError(f"non-canonical JSON pointer: {self.value!r}")
            if pointer:
                _validate_json_pointer(pointer, source=self.value)
        if "^" in anchor:
            target, marker, raw_index = anchor.partition("^")
            if (
                marker != "^"
                or not valid_execution_id(target)
                or not _canonical_index(raw_index)
                or not separator
                or not pointer
            ):
                raise ValueError(f"invalid control value pointer: {self.value!r}")
            return
        target, *indices = anchor.split(".")
        if not valid_execution_id(target) or any(
            not _canonical_index(index) for index in indices
        ):
            raise ValueError(f"invalid value pointer: {self.value!r}")

    @property
    def anchor(self) -> str:
        """Return the run, step, or control anchor."""

        return self.value.partition("/")[0]

    @property
    def pointer(self) -> str | None:
        """Return the RFC 6901 suffix without its leading slash."""

        _anchor, separator, pointer = self.value.partition("/")
        return pointer if separator else None

    def __str__(self) -> str:
        return self.value

    def select(self, *path: str | int) -> Pointer:
        """Return a pointer to a value nested below this pointer."""

        if not path:
            return self
        suffix = _pointer_suffix(path)
        separator = "" if self.value.endswith("/") else "/"
        return Pointer(f"{self.value}{separator}{suffix}")

    @classmethod
    def run(cls, run_id: RunId, *path: str | int) -> Pointer:
        """Point to one run output or a value within it."""

        return cls(_pointer_value(run_id, path))

    @classmethod
    def step(cls, step: StepPath, *path: str | int) -> Pointer:
        """Point to one step output or a value within it."""

        anchor = ".".join((step.run, *(str(index) for index in step.indices)))
        return cls(_pointer_value(anchor, path))

    @classmethod
    def control(
        cls,
        target: str,
        index: int,
        name: str,
        *path: str | int,
    ) -> Pointer:
        """Point to one named local accepted by a run control."""

        return cls(_pointer_value(f"{target}^{index}", (name, *path)))

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: Any,
        _handler: Any,
    ) -> core_schema.CoreSchema:
        return core_schema.json_or_python_schema(
            json_schema=core_schema.no_info_after_validator_function(
                cls,
                core_schema.str_schema(),
            ),
            python_schema=core_schema.union_schema(
                [
                    core_schema.is_instance_schema(cls),
                    core_schema.no_info_after_validator_function(
                        cls,
                        core_schema.str_schema(),
                    ),
                ]
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(
                str,
                return_schema=core_schema.str_schema(),
            ),
        )


@dataclass(frozen=True, slots=True)
class TypedPointer:
    """One value pointer paired with its expected resolved type."""

    type: str
    pointer: Pointer

    def __post_init__(self) -> None:
        validate_type(self.type)
        if not isinstance(self.pointer, Pointer):
            raise TypeError("typed pointer requires a Pointer")


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


@dataclass(frozen=True, slots=True)
class Local:
    """One runtime value and its local-table binding semantics."""

    value: Value | TypedPointer
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
        if not isinstance(self.value, TypedPointer):
            object.__setattr__(
                self,
                "value",
                value_for_type(value_type(self.value), self.value),
            )
        validate_runtime_value(self.value, self.type)
        if self.dim == 1 and not self.type.endswith("[]"):
            raise ValueError("dim=1 requires an array value type")
        if self.dim == 1 and not isinstance(self.value, Array | TypedPointer):
            raise TypeError("dim=1 requires an array value or whole-value pointer")

    @property
    def type(self) -> str:
        """Return the canonical runtime or expected pointer type."""

        if isinstance(self.value, TypedPointer):
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
            from .records import local_from_protocol_data

            return local_from_protocol_data(cast(Mapping[str, object], value))
        raise TypeError("local must be a Local or canonical object")

    @staticmethod
    def _serialize_pydantic(value: Local) -> dict[str, object]:
        from .records import local_to_protocol_data

        return local_to_protocol_data(value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: Any,
        _handler: Any,
    ) -> core_schema.CoreSchema:
        return core_schema.json_or_python_schema(
            json_schema=core_schema.no_info_plain_validator_function(
                cls._validate_pydantic
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


def validate_runtime_value(
    value: object, type_name: str, *, path: str = "local"
) -> None:
    """Validate one concrete or referenced execution value against a type."""

    validate_type(type_name)
    if isinstance(value, TypedPointer):
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


def value_for_type(type_name: str, value: object) -> Value | TypedPointer:
    """Normalize one value at an explicit Toolang typed boundary."""

    validate_type(type_name)
    if isinstance(value, TypedPointer):
        result: object = value
    elif isinstance(value, Pointer):
        result = TypedPointer(type_name, value)
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
    return cast(Value | TypedPointer, result)


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
    if isinstance(value, (TypedPointer, *_PART_TYPES)):
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
class ControlRef:
    """Reference one globally addressed durable control."""

    target: str
    index: int

    def __post_init__(self) -> None:
        if not valid_execution_id(self.target):
            raise ValueError(f"invalid control target: {self.target!r}")
        if self.index < 0:
            raise ValueError("control index must be non-negative")

    @property
    def run(self) -> str:
        """Return the target when this reference is used for a run control."""

        return self.target

    @property
    def thread(self) -> str:
        """Return the target when this reference is used for a thread control."""

        return self.target


@dataclass(frozen=True, slots=True)
class StepPath:
    """Globally address one step within its owning run."""

    run: RunId
    indices: tuple[int, ...]

    def __post_init__(self) -> None:
        if not valid_execution_id(self.run):
            raise ValueError(f"invalid step run id: {self.run!r}")
        if not self.indices or any(index < 0 for index in self.indices):
            raise ValueError("step path requires non-negative indices")

    @classmethod
    def parse(cls, value: StepPath | str) -> StepPath:
        """Parse one canonical step path."""

        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise TypeError("step path must be a string")
        run, separator, suffix = value.partition("/")
        if not separator or not run or not suffix:
            raise ValueError(f"invalid step path: {value!r}")
        raw_indices = suffix.split("/")
        if any(
            not item.isascii() or not item.isdigit() or str(int(item)) != item
            for item in raw_indices
        ):
            raise ValueError(f"invalid step path: {value!r}")
        return cls(run=run, indices=tuple(int(item) for item in raw_indices))

    @classmethod
    def from_local(cls, run: RunId, path: str) -> StepPath:
        """Build one step path from separately stored run and local path."""

        return cls.parse(f"{run}/{path}")

    @property
    def local(self) -> str:
        """Return the run-local index path."""

        return "/".join(str(index) for index in self.indices)

    @property
    def parent(self) -> StepPath | None:
        """Return the enclosing step within the same run."""

        if len(self.indices) == 1:
            return None
        return StepPath(self.run, self.indices[:-1])

    @property
    def index(self) -> int:
        """Return the final step index."""

        return self.indices[-1]

    def child(self, index: int) -> StepPath:
        """Return one direct child step."""

        return StepPath(self.run, (*self.indices, index))

    def __str__(self) -> str:
        return f"{self.run}/{self.local}"

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: Any,
        _handler: Any,
    ) -> core_schema.CoreSchema:
        return core_schema.json_or_python_schema(
            json_schema=core_schema.no_info_after_validator_function(
                cls.parse,
                core_schema.str_schema(),
            ),
            python_schema=core_schema.union_schema(
                [
                    core_schema.is_instance_schema(cls),
                    core_schema.no_info_after_validator_function(
                        cls.parse,
                        core_schema.str_schema(),
                    ),
                ]
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(
                str,
                return_schema=core_schema.str_schema(),
            ),
        )


def _parse_execution_error(value: object) -> str | Pointer:
    if isinstance(value, Pointer | str):
        return value
    if isinstance(value, Mapping):
        payload = cast(Mapping[str, object], value)
        tag = payload.get("?") if set(payload) == {"?"} else None
        if isinstance(tag, str) and tag.startswith("@") and len(tag) > 1:
            return Pointer(tag[1:])
    raise ValueError("invalid execution error")


def _serialize_execution_error(error: str | Pointer) -> str | dict[str, str]:
    return error if isinstance(error, str) else {"?": f"@{error}"}


ExecutionError: TypeAlias = Annotated[
    str | Pointer,
    BeforeValidator(_parse_execution_error),
    PlainSerializer(_serialize_execution_error, return_type=object),
]

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
    "system",
]
ControlTiming = Literal["immediate", "next_step", "next_call"]
ControlKind = Literal[
    "start",
    "rerun",
    "retry",
    "steer",
    "stop",
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


def validate_execution_id(value: object, *, label: str) -> str:
    """Return one canonical run or thread id, or reject it before persistence."""

    if not valid_execution_id(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return cast(str, value)


def _canonical_index(value: str) -> bool:
    return (
        bool(value) and value.isascii() and value.isdigit() and str(int(value)) == value
    )


def _validate_json_pointer(value: str, *, source: str) -> None:
    index = 0
    while index < len(value):
        if value[index] != "~":
            index += 1
            continue
        if index + 1 >= len(value) or value[index + 1] not in {"0", "1"}:
            raise ValueError(f"invalid JSON pointer escape: {source!r}")
        index += 2


def _pointer_value(anchor: str, path: Sequence[str | int]) -> str:
    if not path:
        return anchor
    return f"{anchor}/{_pointer_suffix(path)}"


def _pointer_suffix(path: Sequence[str | int]) -> str:
    return "/".join(str(item).replace("~", "~0").replace("/", "~1") for item in path)
