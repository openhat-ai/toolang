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
    "value_type",
]
