"""Shared model discovery, alias, and execution value types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
