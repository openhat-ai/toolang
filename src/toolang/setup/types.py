"""Immutable runtime setup types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
import platform
from types import MappingProxyType

from toolang.base.protocols.model import ModelAdapter, ModelProvider
from toolang.base.protocols.tool import AgentTool
from toolang.base.types.model import ModelCatalogSnapshot, ModelInfo, Provider
from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits
from toolang.common.layout import AgentLayout


@dataclass(frozen=True, slots=True)
class AgentEnvironment:
    """Safe process-environment facts captured where the agent actually runs."""

    sandbox: str
    system: str
    release: str
    machine: str
    container: bool
    root: Path
    home: Path
    working_directory: Path

    @classmethod
    def capture(
        cls,
        layout: AgentLayout,
        *,
        envs: Mapping[str, str],
    ) -> AgentEnvironment:
        """Capture non-secret environment facts from explicit setup inputs."""

        sandbox = envs.get("TOOLANG_SANDBOX", "none").strip() or "none"
        return cls(
            sandbox=sandbox,
            system=platform.system(),
            release=platform.release(),
            machine=platform.machine(),
            container=sandbox.partition(":")[0] == "docker",
            root=layout.root,
            home=layout.home,
            working_directory=Path.cwd().resolve(),
        )


@dataclass(frozen=True, slots=True)
class AgentSetup:
    """Effective immutable runtime setup fixed for one root run."""

    layout: AgentLayout
    providers: Mapping[str, ModelProvider | Provider]
    adapters: Mapping[str, ModelAdapter]
    models: tuple[ModelInfo, ...]
    tools: Mapping[str, AgentTool]
    envs: Mapping[str, str]
    catalog: ModelCatalogSnapshot | None = None
    provider_configs: Mapping[str, object] = field(default_factory=dict)
    environment: AgentEnvironment | None = None
    ceiling: AgentCeiling = AgentCeiling()
    bindings: RunBindings = RunBindings()
    limits: RunLimits = RunLimits()

    def __post_init__(self) -> None:
        providers = dict(self.providers)
        adapters = dict(self.adapters)
        models = tuple(self.models)
        if not isinstance(self.ceiling, AgentCeiling):
            raise TypeError("setup agent ceiling must be AgentCeiling")
        if not isinstance(self.bindings, RunBindings):
            raise TypeError("setup bindings must be RunBindings")
        if not isinstance(self.limits, RunLimits):
            raise TypeError("setup limits must be RunLimits")
        for key, provider in providers.items():
            identity = (
                provider.id
                if isinstance(provider, Provider)
                else getattr(provider, "name", key)
            )
            if key != identity:
                raise ValueError(
                    f"provider mapping key {key!r} does not match {identity!r}"
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
        object.__setattr__(
            self,
            "provider_configs",
            MappingProxyType(dict(self.provider_configs)),
        )
