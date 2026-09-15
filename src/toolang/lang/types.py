"""Language-owned runtime value vocabulary."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
import math
import re
from types import MappingProxyType
from typing import Generic, TypeAlias, TypeVar, cast, overload

from typing_extensions import TypeAliasType

from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Part,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)


Text: TypeAlias = str
Number: TypeAlias = int | float
Boolean: TypeAlias = bool

_VALUE_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[\])*$")
_RUNNABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_UNNAMED_RUNNABLE_RE = re.compile(
    r"^<(?P<role>entry|adhoc)(?::(?P<line>[1-9][0-9]*))?>$"
)
_PART_TYPES = (
    TextPart,
    ImagePart,
    AudioPart,
    DocumentPart,
    ToolCallPart,
    ToolResultPart,
)
_RESERVED_STRUCT_TYPES = frozenset(
    {
        "Text",
        "Number",
        "Boolean",
        "Json",
        "Part",
        *(part_type.__name__ for part_type in _PART_TYPES),
    }
)

_T_co = TypeVar("_T_co", covariant=True)


def validate_type(type_name: str) -> str:
    """Return one canonical Toolang value type."""

    if not isinstance(type_name, str) or not _VALUE_TYPE_RE.fullmatch(type_name):
        raise ValueError(f"invalid Toolang value type: {type_name!r}")
    return type_name


def validate_struct_type(type_name: str) -> str:
    """Return one unambiguous authored struct type."""

    validate_type(type_name)
    if type_name in _RESERVED_STRUCT_TYPES:
        raise ValueError(f"struct type conflicts with built-in type: {type_name}")
    return type_name


@dataclass(frozen=True, slots=True)
class RunnableRef:
    """One parsed runnable reference, including optional module and kind."""

    name: str
    kind: str | None = None
    module: str | None = None

    @property
    def role(self) -> str | None:
        match = _UNNAMED_RUNNABLE_RE.fullmatch(self.name)
        return None if match is None else match.group("role")

    @property
    def line(self) -> int | None:
        match = _UNNAMED_RUNNABLE_RE.fullmatch(self.name)
        if match is None or match.group("line") is None:
            return None
        return int(match.group("line"))


def parse_runnable_ref_parts(value: str) -> RunnableRef:
    """Parse one runnable reference: optional module, optional kind, name."""

    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError(f"invalid public runnable ref: {value!r}")
    module: str | None = None
    runnable = value
    if "::" in value:
        module, runnable = value.rsplit("::", 1)
        if not module or not runnable:
            raise ValueError(f"invalid public runnable ref: {value!r}")
        parts = module.split("::")
        if any(not _RUNNABLE_NAME_RE.fullmatch(part) for part in parts):
            raise ValueError(f"invalid public runnable ref: {value!r}")
    kind: str | None = None
    name = runnable
    if not name.startswith("<") and ":" in name:
        head, rest = name.split(":", 1)
        if head in {"agic", "flow"}:
            kind = head
            name = rest
    if _RUNNABLE_NAME_RE.fullmatch(name):
        return RunnableRef(name=name, kind=kind, module=module)
    if _UNNAMED_RUNNABLE_RE.fullmatch(name) is None:
        raise ValueError(f"invalid public runnable ref: {value!r}")
    return RunnableRef(name=name, kind=kind, module=module)


def parse_public_runnable_ref(value: str) -> tuple[str, str | None]:
    """Parse one exact public runnable reference owned by the language."""

    parsed = parse_runnable_ref_parts(value)
    return parsed.name, parsed.kind


def display_runnable_ref(value: str, *, surface: str) -> str:
    """Return a surface-specific unnamed/named runnable label."""

    parsed = parse_runnable_ref_parts(value)
    if parsed.role is None:
        kind = f"{parsed.kind}:" if parsed.kind else ""
        return f"{kind}{parsed.name}"
    kind = parsed.kind or "agic"
    if surface in {"help", "chat"}:
        return f"{kind}:<{parsed.role}>"
    if surface == "progress":
        if parsed.line is None:
            return f"{kind}:<{parsed.role}>"
        return f"{kind}:<{parsed.role}:{parsed.line}>"
    return value


def unnamed_runnable_name(role: str, line: int) -> str:
    """Return the lined unnamed lookup key for one declaration."""

    if role not in {"entry", "adhoc"}:
        raise ValueError(f"invalid unnamed runnable role: {role!r}")
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        raise ValueError(f"invalid unnamed runnable line: {line!r}")
    return f"<{role}:{line}>"


@dataclass(frozen=True, slots=True)
class Array(Sequence[_T_co], Generic[_T_co]):
    """One immutable typed Toolang array."""

    type: str
    value: tuple[_T_co, ...]

    def __post_init__(self) -> None:
        validate_type(self.type)
        if not self.type.endswith("[]"):
            raise ValueError("array type must end in []")
        object.__setattr__(
            self,
            "value",
            tuple(cast(_T_co, _snapshot_container(item)) for item in self.value),
        )

    @property
    def item_type(self) -> str:
        """Return the array item type."""

        return self.type[:-2]

    @overload
    def __getitem__(self, index: int) -> _T_co: ...

    @overload
    def __getitem__(self, index: slice) -> Array[_T_co]: ...

    def __getitem__(self, index: int | slice) -> _T_co | Array[_T_co]:
        if isinstance(index, slice):
            return Array(self.type, self.value[index])
        return self.value[index]

    def __iter__(self) -> Iterator[_T_co]:
        return iter(self.value)

    def __len__(self) -> int:
        return len(self.value)


@dataclass(frozen=True, slots=True)
class Struct(Mapping[str, _T_co], Generic[_T_co]):
    """One immutable authored Toolang struct value."""

    type: str
    value: Mapping[str, _T_co]

    def __post_init__(self) -> None:
        validate_struct_type(self.type)
        if self.type.endswith("[]"):
            raise ValueError("struct type cannot be an array")
        if not all(isinstance(name, str) for name in self.value):
            raise TypeError("struct field names must be strings")
        object.__setattr__(
            self,
            "value",
            MappingProxyType(
                {
                    name: cast(_T_co, _snapshot_container(item))
                    for name, item in self.value.items()
                }
            ),
        )

    def __getitem__(self, name: str) -> _T_co:
        return self.value[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self.value)

    def __len__(self) -> int:
        return len(self.value)


def _snapshot_container(value: object) -> object:
    if isinstance(value, Array | Struct):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType(
            {name: _snapshot_container(item) for name, item in value.items()}
        )
    if isinstance(value, tuple | list):
        return tuple(_snapshot_container(item) for item in value)
    return value


Json = TypeAliasType(
    "Json",
    None
    | Text
    | Number
    | Boolean
    | tuple["Json", ...]
    | list["Json"]
    | Mapping[str, "Json"],
)

Value = TypeAliasType(
    "Value",
    Text
    | Number
    | Boolean
    | None
    | Part
    | Array["Value"]
    | Struct["Value"]
    | tuple["Value", ...]
    | list["Value"]
    | Mapping[str, "Value"],
)
"""One concrete Toolang value.

Type descriptions use ``T`` for any declared type and ``S`` for an authored
Toolang ``struct``. ``Json`` is an unknown typed boundary; scalar Json values
normalize to their concrete runtime scalar types.
"""


def value_type(value: object) -> str:
    """Return the canonical runtime type of one concrete value."""

    if isinstance(value, bool):
        return "Boolean"
    if isinstance(value, str):
        return "Text"
    if isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            raise TypeError("Number values must be finite")
        return "Number"
    if isinstance(value, _PART_TYPES):
        return type(value).__name__
    if isinstance(value, Array | Struct):
        return value.type
    if value is None or isinstance(value, Mapping | tuple | list):
        return "Json"
    raise TypeError(f"unsupported Toolang value: {type(value).__name__}")


__all__ = [
    "Array",
    "Boolean",
    "Json",
    "Number",
    "Part",
    "Struct",
    "Text",
    "Value",
    "validate_type",
    "validate_struct_type",
    "parse_public_runnable_ref",
    "parse_runnable_ref_parts",
    "RunnableRef",
    "display_runnable_ref",
    "unnamed_runnable_name",
    "value_type",
]
