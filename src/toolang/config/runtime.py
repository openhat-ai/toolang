"""Explicit process configuration resolved before runtime startup."""

from __future__ import annotations

from collections.abc import Mapping


class RuntimeConfig:
    """Mutable process configuration assembled at the process boundary."""

    def __init__(self, values: Mapping[str, object] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, key: str, default: object | None = None) -> object | None:
        return self._values.get(key, default)

    def require(self, key: str) -> object:
        if key not in self._values:
            raise KeyError(f"missing config: {key}")
        return self._values[key]

    def set(self, key: str, value: object) -> None:
        self._values[key] = value

    def snapshot(self) -> dict[str, object]:
        return dict(self._values)
