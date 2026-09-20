"""Installed plugin identities, sources, and loading results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PluginSource = Literal["built-in", "external"]


@dataclass(frozen=True, slots=True)
class PluginInfo:
    """One discoverable plugin entry point."""

    name: str
    source: PluginSource


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    """One loaded plugin instance with its authority source."""

    entry_point_name: str
    name: str
    plugin: object
    source: PluginSource


@dataclass(frozen=True, slots=True)
class PluginProvenance:
    """Stable installed-code identity for one plugin entry point."""

    name: str
    value: str
    distribution: str | None
    version: str | None

    def to_data(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "value": self.value,
            "distribution": self.distribution,
            "version": self.version,
        }
