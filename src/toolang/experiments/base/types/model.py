"""Shared model target and binding value types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..protocols.model import ModelPlugin


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """One fully resolved model target for one runtime call."""

    ref: str
    plugin: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelBinding:
    """One resolved model target plus its loaded plugin."""

    target: ResolvedModel
    plugin: ModelPlugin


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """One model plugin capability snapshot."""

    tools: bool = True
    streaming: bool = True
