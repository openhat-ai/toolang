"""Installed runtime implementations available to one agent process."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from toolang.base.protocols.model import ModelAdapter, ModelProvider
from toolang.base.protocols.tool import AgentTool


@dataclass(frozen=True, slots=True)
class AgentSetup:
    """Immutable process wiring captured by accepted runs."""

    name: str
    home: Path
    tools: Mapping[str, AgentTool]
    model_providers: Mapping[str, ModelProvider]
    model_adapters: Mapping[str, ModelAdapter]
    model_environ: Mapping[str, str]
    model_selectors: tuple[str, ...] = ()
    model_cache_dir: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", MappingProxyType(dict(self.tools)))
        object.__setattr__(
            self,
            "model_providers",
            MappingProxyType(dict(self.model_providers)),
        )
        object.__setattr__(
            self,
            "model_adapters",
            MappingProxyType(dict(self.model_adapters)),
        )
        object.__setattr__(
            self,
            "model_environ",
            MappingProxyType(dict(self.model_environ)),
        )
        object.__setattr__(self, "model_selectors", tuple(self.model_selectors))
