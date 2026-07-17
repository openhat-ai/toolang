"""Installed runtime implementations available to one agent execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from toolang.base.protocols.model import ModelAdapter, ModelProvider
from toolang.base.protocols.tool import AgentTool


@dataclass(frozen=True, slots=True)
class AgentSetup:
    """Immutable installed implementations captured by accepted runs."""

    tools: Mapping[str, AgentTool]
    model_providers: Mapping[str, ModelProvider]
    model_adapters: Mapping[str, ModelAdapter]

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
