"""Language-owned runtime value vocabulary."""

from __future__ import annotations

from collections.abc import Mapping
from typing_extensions import TypeAliasType

from toolang.base.types.message import MessagePart


Value = TypeAliasType(
    "Value",
    str
    | int
    | float
    | bool
    | None
    | MessagePart
    | tuple["Value", ...]
    | list["Value"]
    | Mapping[str, "Value"],
)
"""One concrete Toolang value.

Type descriptions use ``T`` for any declared type and ``S`` for an authored
Toolang ``struct``. ``Json`` is the unknown type at a typed boundary; it is not
a separate Python wrapper.
"""


__all__ = ["Value"]
