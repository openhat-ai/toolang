"""Small helpers for immutable snapshot containers."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


def freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Copy a mapping into recursively immutable containers."""

    return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})


def mutable_data(value: Any) -> Any:
    """Copy immutable containers into mutable data containers."""

    if isinstance(value, Mapping):
        return {str(key): mutable_data(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [mutable_data(item) for item in value]
    if isinstance(value, set | frozenset):
        return [mutable_data(item) for item in sorted(value, key=repr)]
    if isinstance(value, bytearray):
        return bytearray(value)
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return freeze_mapping(value)
    if isinstance(value, tuple | list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)
    if isinstance(value, bytearray):
        return bytes(value)
    return value
