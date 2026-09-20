"""Immutable runtime setup types."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
import platform
from types import MappingProxyType

from toolang.base.protocols.model import ModelAdapter
from toolang.base.types.model import ModelCatalogSnapshot, ModelOverride, Provider
from toolang.base.types.policy import RunDefaults, RunLimits
from toolang.common.layout import AgentLayout
from toolang.plugin.models.collections import ModelCollection
from toolang.plugin.toolsets.collections import ToolCollection


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
        sandbox: str,
    ) -> AgentEnvironment:
        """Capture non-secret environment facts from explicit setup inputs."""

        if not sandbox or sandbox != sandbox.strip():
            raise ValueError("agent environment requires a canonical sandbox")
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
    providers: Mapping[str, Provider]
    adapters: Mapping[str, ModelAdapter]
    models: ModelCollection
    tools: ToolCollection
    envs: Mapping[str, str]
    revision: str = ""
    environment: AgentEnvironment | None = None
    defaults: RunDefaults = RunDefaults()
    limits: RunLimits = RunLimits()
    compact_model: ModelOverride | None = None
    catalog_sources: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    _catalog_loader: Callable[[], ModelCatalogSnapshot] | None = field(
        default=None, repr=False, compare=False
    )

    def model_catalog(self, *, all: bool = False) -> ModelCatalogSnapshot:
        """Read the default view, or materialize this version's complete catalog."""

        if all and self._catalog_loader is not None:
            return self._catalog_loader()
        return ModelCatalogSnapshot(
            providers=self.providers,
            models=self.models.entries,
            revision=self.revision,
        )

    def __post_init__(self) -> None:
        providers = dict(self.providers)
        adapters = dict(self.adapters)
        if not isinstance(self.models, ModelCollection):
            raise TypeError("setup models must be ModelCollection")
        if not isinstance(self.tools, ToolCollection):
            raise TypeError("setup tools must be ToolCollection")
        if not isinstance(self.defaults, RunDefaults):
            raise TypeError("setup defaults must be RunDefaults")
        if not isinstance(self.limits, RunLimits):
            raise TypeError("setup limits must be RunLimits")
        for key, provider in providers.items():
            identity = provider.id
            if key != identity:
                raise ValueError(
                    f"provider mapping key {key!r} does not match {identity!r}"
                )
        missing_providers = {
            entry._toolang.provider
            for entry in self.models.entries
            if entry._toolang.provider not in providers
        }
        if missing_providers:
            raise ValueError(
                "setup models reference unknown providers: "
                + ", ".join(sorted(missing_providers))
            )
        object.__setattr__(
            self, "catalog_sources", MappingProxyType(dict(self.catalog_sources))
        )
        object.__setattr__(self, "providers", MappingProxyType(providers))
        object.__setattr__(self, "adapters", MappingProxyType(adapters))
        object.__setattr__(self, "envs", MappingProxyType(dict(self.envs)))
