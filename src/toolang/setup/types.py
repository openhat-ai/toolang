"""Immutable setup generations with synchronous, lazy resource accessors."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import platform
from threading import Lock
from types import MappingProxyType
from typing import TypeVar, cast, Literal

from toolang.base.protocols.model import ModelAdapter, ModelCatalog
from toolang.base.protocols.tool import Toolset
from toolang.base.types.model import Model, ModelOverride, Provider
from toolang.base.types.policy import RunDefaults, RunLimits
from toolang.common.layout import AgentLayout, IMPLICIT_WORKSPACE_NAME
from .messaging import MessagingSetup
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
    workspace_mounts: Mapping[str, tuple[Path, Path]] = field(default_factory=dict)
    workspace_location: Literal["host", "guest"] = "host"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "workspace_mounts", MappingProxyType(dict(self.workspace_mounts))
        )

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
        location = os.environ.get(
            "TOOLANG_WORKSPACE_LOCATION",
            "host" if sandbox.partition(":")[0] == "host" else "guest",
        )
        if location not in {"host", "guest"}:
            raise ValueError("invalid sandbox workspace location")
        raw_mounts = json.loads(
            os.environ.get("TOOLANG_WORKSPACE_MOUNTS", "{}")
            if location == "guest"
            else "{}"
        )
        if not isinstance(raw_mounts, dict):
            raise ValueError("sandbox workspace mounts must be an object")
        mounts: dict[str, tuple[Path, Path]] = {}
        if location == "guest":
            for name, roots in raw_mounts.items():
                if (
                    not isinstance(name, str)
                    or not isinstance(roots, list)
                    or len(roots) != 2
                    or not all(isinstance(root, str) for root in roots)
                    or not all(Path(root).is_absolute() for root in roots)
                ):
                    raise ValueError("invalid sandbox workspace mount mapping")
                mounts[name] = Path(roots[0]), Path(roots[1])
        return cls(
            sandbox=sandbox,
            system=platform.system(),
            release=platform.release(),
            machine=platform.machine(),
            container=sandbox.partition(":")[0] == "docker",
            root=layout.root,
            home=layout.home,
            working_directory=Path.cwd().resolve(),
            workspace_mounts=mounts,
            workspace_location=cast(Literal["host", "guest"], location),
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
class CompactConfig:
    """Captured compaction settings; fractional sizes refer to thread context."""

    model: ModelOverride | None = None
    summary: int | float = 4096
    recent: int | float = 0.30
    trigger: int | float = 0.80


@dataclass(frozen=True, slots=True)
class AgentSetup:
    """One immutable setup generation with independently lazy resources."""

    layout: AgentLayout
    envs: Mapping[str, str]
    revision: str = ""
    environment: AgentEnvironment | None = None
    defaults: RunDefaults = RunDefaults()
    limits: RunLimits = RunLimits()
    compact: CompactConfig = CompactConfig()
    messaging: MessagingSetup | None = None
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

    def workspace_grants(self, grants: Mapping[str, str]) -> dict[str, str]:
        """Add the implicit lab grant before configured workspaces; first name wins."""
        scratch_source = str(self.layout.home / IMPLICIT_WORKSPACE_NAME)
        if (
            self.environment is not None
            and self.environment.workspace_location == "guest"
        ):
            captured = self.environment.workspace_mounts.get(IMPLICIT_WORKSPACE_NAME)
            if captured is not None:
                scratch_source = str(captured[0])
        result = {IMPLICIT_WORKSPACE_NAME: scratch_source}
        result.update(
            (name, source)
            for name, source in grants.items()
            if name != IMPLICIT_WORKSPACE_NAME
        )
        return result

    def workspace_roots(self, grants: Mapping[str, str]) -> dict[str, Path]:
        """Intersect ordered workspace grants with mounts captured by hosting."""
        ordered = self.workspace_grants(grants)
        if self.environment is None or self.environment.workspace_location == "host":
            return {name: Path(source) for name, source in ordered.items()}
        result = {}
        for name, source in ordered.items():
            mounted = self.environment.workspace_mounts.get(name)
            if mounted is not None and mounted[0] == Path(source):
                result[name] = mounted[1]
        return result

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
