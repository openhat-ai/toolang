"""Immutable runtime setup types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from toolang.base.protocols.model import ModelAdapter, ModelProvider
from toolang.base.protocols.tool import AgentTool
from toolang.base.types.model import ModelInfo
from toolang.common.layout import AgentLayout


@dataclass(frozen=True, slots=True)
class AgentSetup:
    """Installed implementations and models fixed for one agent run."""

    layout: AgentLayout
    providers: Mapping[str, ModelProvider]
    adapters: Mapping[str, ModelAdapter]
    models: tuple[ModelInfo, ...]
    tools: Mapping[str, AgentTool]
    envs: Mapping[str, str]

    def __post_init__(self) -> None:
        providers = dict(self.providers)
        adapters = dict(self.adapters)
        models = tuple(self.models)
        for key, provider in providers.items():
            if key != provider.name:
                raise ValueError(
                    f"provider mapping key {key!r} does not match {provider.name!r}"
                )
        identities = [(model.provider, model.ref) for model in models]
        if len(identities) != len(set(identities)):
            raise ValueError("setup models must be unique by provider and ref")
        missing_providers = {
            model.provider for model in models if model.provider not in providers
        }
        if missing_providers:
            raise ValueError(
                "setup models reference unknown providers: "
                + ", ".join(sorted(missing_providers))
            )
        object.__setattr__(self, "providers", MappingProxyType(providers))
        object.__setattr__(self, "adapters", MappingProxyType(adapters))
        object.__setattr__(self, "models", models)
        object.__setattr__(self, "tools", MappingProxyType(dict(self.tools)))
        object.__setattr__(self, "envs", MappingProxyType(dict(self.envs)))
