"""Immutable setup generations with synchronous, lazy resource accessors."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
import platform
from threading import Lock
from types import MappingProxyType
from typing import TypeVar, cast

from toolang.base.protocols.model import ModelAdapter, ModelCatalog
from toolang.base.protocols.tool import Toolset
from toolang.base.types.model import Model, ModelOverride, Provider
from toolang.base.types.policy import RunDefaults, RunLimits
from toolang.common.layout import AgentLayout
from toolang.plugin.toolsets.collections import ToolCollection
from toolang.plugin.types import LoadedPlugin

T = TypeVar("T")


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
class _ModelData:
    """Memoized, immutable reference views over one setup generation."""

    models: tuple[Model, ...]
    providers: tuple[Provider, ...]
    models_effective: tuple[Model, ...]
    providers_effective: tuple[Provider, ...]


@dataclass(slots=True)
class _LazyValues:
    """Thread-safe single-flight memoization scoped to one AgentSetup object."""

    lock: Lock = field(default_factory=Lock)
    values: dict[str, object] = field(default_factory=dict)
    loading: dict[str, Future[object]] = field(default_factory=dict)

    def get(self, key: str, loader: Callable[[], T]) -> T:
        with self.lock:
            if key in self.values:
                return cast(T, self.values[key])
            future = self.loading.get(key)
            owner = future is None
            if future is None:
                future = Future()
                self.loading[key] = future

        if not owner:
            return cast(T, future.result())

        try:
            value = loader()
        except BaseException as error:
            with self.lock:
                if self.loading.get(key) is future:
                    self.loading.pop(key, None)
            future.set_exception(error)
            raise

        with self.lock:
            self.values[key] = value
            if self.loading.get(key) is future:
                self.loading.pop(key, None)
        future.set_result(value)
        return value


@dataclass(frozen=True, slots=True)
class AgentSetup:
    """One immutable setup generation with independently lazy resources."""

    layout: AgentLayout
    envs: Mapping[str, str]
    revision: str = ""
    environment: AgentEnvironment | None = None
    defaults: RunDefaults = RunDefaults()
    limits: RunLimits = RunLimits()
    compact_model: ModelOverride | None = None
    catalog_sources: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    _load_models: Callable[[AgentSetup], _ModelData] = field(
        default=lambda _setup: _empty_model_data(), repr=False, compare=False
    )
    _load_tools: Callable[[Mapping[str, LoadedPlugin]], ToolCollection] = field(
        default=lambda _plugins: ToolCollection(), repr=False, compare=False
    )
    _allowed_tools: tuple[str, ...] | None = None
    _load_adapters: Callable[[], Mapping[str, ModelAdapter]] = field(
        default=lambda: {}, repr=False, compare=False
    )
    _load_catalogs: Callable[[], Mapping[str, ModelCatalog]] = field(
        default=lambda: {}, repr=False, compare=False
    )
    _load_toolset_plugins: Callable[[], Mapping[str, LoadedPlugin]] = field(
        default=lambda: {}, repr=False, compare=False
    )
    _lazy: _LazyValues = field(default_factory=_LazyValues, repr=False, compare=False)

    def models(self) -> tuple[Model, ...]:
        """Load and memoize every ordered model record, including unavailable ones."""

        return self._model_data().models

    def providers(self) -> tuple[Provider, ...]:
        """Load and memoize every provider in catalog-file order."""

        return self._model_data().providers

    def models_effective(self) -> tuple[Model, ...]:
        """Return routable, allowed model records in policy order."""

        return self._model_data().models_effective

    def providers_effective(self) -> tuple[Provider, ...]:
        """Return providers used by effective models in catalog-file order."""

        return self._model_data().providers_effective

    def model_allowed(self, ref: str) -> bool:
        """Return allow-policy membership independently of route readiness."""

        return any(
            model.ref == ref and model._toolang.allowed
            for model in self._model_data().models
        )

    def tools(self, *, all: bool = False) -> ToolCollection:
        """Load effective tools, or the complete pre-allow set."""

        complete = self._lazy.get(
            "tools:all",
            lambda: self._load_tools(self._toolset_plugins()),
        )
        if all or self._allowed_tools is None:
            return complete
        queries = self._allowed_tools
        assert queries is not None
        return self._lazy.get(
            "tools:effective", lambda: _filter_tools(complete, queries)
        )

    def toolsets(self) -> Mapping[str, Toolset]:
        """Load toolset plugins without materializing their leaf tools."""

        return self._lazy.get("toolsets", self._public_toolsets)

    def adapters(self) -> Mapping[str, ModelAdapter]:
        """Load model-adapter plugins without materializing model data."""

        return self._lazy.get(
            "adapters", lambda: MappingProxyType(dict(self._load_adapters()))
        )

    def catalogs(self) -> Mapping[str, ModelCatalog]:
        """Load model-catalog plugins without resolving their model data."""

        return self._lazy.get(
            "catalogs", lambda: MappingProxyType(dict(self._load_catalogs()))
        )

    def _toolset_plugins(self) -> Mapping[str, LoadedPlugin]:
        return self._lazy.get(
            "toolset_plugins",
            lambda: MappingProxyType(dict(self._load_toolset_plugins())),
        )

    def _public_toolsets(self) -> Mapping[str, Toolset]:
        return MappingProxyType(
            {
                name: cast(Toolset, value.plugin)
                for name, value in self._toolset_plugins().items()
            }
        )

    def _model_data(self) -> _ModelData:
        return self._lazy.get("models", lambda: self._load_models(self))

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "catalog_sources", MappingProxyType(dict(self.catalog_sources))
        )
        object.__setattr__(self, "envs", MappingProxyType(dict(self.envs)))
        if not isinstance(self.defaults, RunDefaults):
            raise TypeError("setup defaults must be RunDefaults")
        if not isinstance(self.limits, RunLimits):
            raise TypeError("setup limits must be RunLimits")


def _filter_tools(
    complete: ToolCollection,
    queries: tuple[str, ...],
) -> ToolCollection:
    """Apply user allow policy while preserving runtime tools."""

    selected = complete.user.match(queries) if queries else ToolCollection()
    return complete.subset((*complete.runtime, *selected)).compact()


def _empty_model_data() -> _ModelData:
    return _ModelData(
        models=(),
        providers=(),
        models_effective=(),
        providers_effective=(),
    )
