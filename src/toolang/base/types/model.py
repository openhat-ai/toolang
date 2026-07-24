"""Shared model discovery, alias, and execution value types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """One provider-scoped model info entry."""

    ref: str
    provider: str
    name: str
    model: str
    selectors: tuple[str, ...] = field(default_factory=tuple)
    adapter: str = "default"
    scope: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    tools: bool = True
    streaming: bool = True
    context_window: int | None = None
    max_output_tokens: int | None = None
    input_price: float | None = None
    output_price: float | None = None
    details: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_selector(self) -> str:
        """Return the preferred selector for display surfaces."""

        for selector in self.selectors:
            text = selector.strip()
            if text:
                return text
        return self.ref

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> Self:
        """Build one model info from persisted protocol-neutral data."""

        selectors = data.get("selectors", ())
        tags = data.get("tags", ())
        metadata = data.get("metadata", {})
        if not isinstance(selectors, list | tuple):
            raise TypeError("model selectors must be a list")
        if not isinstance(tags, list | tuple):
            raise TypeError("model tags must be a list")
        if not isinstance(metadata, Mapping):
            raise TypeError("model metadata must be an object")
        return cls(
            ref=str(data["ref"]),
            provider=str(data["provider"]),
            name=str(data["name"]),
            model=str(data["model"]),
            selectors=tuple(str(item) for item in selectors),
            adapter=str(data.get("adapter") or "default"),
            scope=str(data["scope"]) if data.get("scope") is not None else None,
            tags=tuple(str(item) for item in tags),
            tools=bool(data.get("tools", True)),
            streaming=bool(data.get("streaming", True)),
            context_window=_optional_int(data.get("context_window")),
            max_output_tokens=_optional_int(data.get("max_output_tokens")),
            input_price=_optional_float(data.get("input_price")),
            output_price=_optional_float(data.get("output_price")),
            details=str(data["details"]) if data.get("details") is not None else None,
            metadata={str(key): value for key, value in metadata.items()},
        )

    def to_data(self) -> dict[str, object]:
        """Return persisted protocol-neutral data for this model."""

        return {
            "ref": self.ref,
            "provider": self.provider,
            "name": self.name,
            "model": self.model,
            "selectors": list(self.selectors),
            "adapter": self.adapter,
            "scope": self.scope,
            "tags": list(self.tags),
            "tools": self.tools,
            "streaming": self.streaming,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "input_price": self.input_price,
            "output_price": self.output_price,
            "details": self.details,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ModelAlias:
    """One named local alias to a selectable model target."""

    name: str
    ref: str
    provider: str
    model: str | None = None
    display_name: str | None = None
    adapter: str | None = None
    endpoint: str | None = None
    key_env: str | None = None
    scope: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    tools: bool | None = None
    streaming: bool | None = None
    headers: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    details: str | None = None


@dataclass(frozen=True, slots=True)
class ModelTarget:
    """One fully resolved execution target for one runtime call."""

    ref: str
    provider: str
    name: str
    model: str
    adapter: str
    base_url: str | None = None
    api_key: str | None = None
    scope: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    headers: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    tools: bool = True
    streaming: bool = True


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("model integer fields must be integers")
    return value


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("model price fields must be numbers")
    return float(value)
