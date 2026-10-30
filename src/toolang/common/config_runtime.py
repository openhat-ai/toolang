"""Runtime configuration overrides and authored source identities."""

from __future__ import annotations

from collections.abc import Mapping, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import cast

from .immutable import freeze_mapping, mutable_data

CONFIG_PATCH_ENV = "TOOLANG_CONFIG_PATCH"
LAUNCH_SCHEMA = 2
_SCOPES = {"root", "home"}
_FILE_ORIGINS = {"root/catalog.json", "home/catalog.json", "program"}


def _absolute(value: object) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or not Path(value).is_absolute()
    ):
        raise ValueError("invalid launch source path")
    if os.path.normpath(value) != value:
        raise ValueError("invalid launch source path")
    return Path(value)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError
    return cast(dict[str, object], value)


def _validate_values(value: object) -> None:
    if isinstance(value, dict):
        for item in _object(value).values():
            _validate_values(item)
    elif isinstance(value, list):
        for item in value:
            _validate_values(item)
    elif (
        not isinstance(value, str | bool | int | float)
        or isinstance(value, float)
        and not math.isfinite(value)
    ):
        raise ValueError


def _merge(
    first: Mapping[str, object], second: Mapping[str, object]
) -> dict[str, object]:
    result = mutable_data(first)
    for key, value in second.items():
        previous = result.get(key)
        result[key] = (
            _merge(previous, cast(Mapping[str, object], value))
            if isinstance(previous, Mapping) and isinstance(value, Mapping)
            else mutable_data(value)
        )
    return result


@dataclass(frozen=True, slots=True)
class ConfigOrigins:
    """Source identities and path resolution, without execution configuration."""

    origins: Mapping[str, Path] = field(default_factory=dict)
    captured: bool = True
    snapshots: Mapping[str, str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "origins", MappingProxyType(dict(self.origins)))
        object.__setattr__(self, "snapshots", MappingProxyType(dict(self.snapshots)))

    def origin(self, scope: str, path: Path) -> Path:
        return self.origins.get(scope, path)

    def resolve_path(self, value: str, directory: Path) -> Path:
        candidate = Path(value)
        reference = os.path.normpath(
            str(candidate if candidate.is_absolute() else directory / candidate)
        )
        captured = self.origins.get(reference)
        if captured is not None:
            return captured
        if self.origins and self.captured:
            if value.startswith("~"):
                raise ValueError("uncaptured source path requires a new launch")
            return Path(reference)
        candidate = candidate.expanduser()
        return (
            candidate if candidate.is_absolute() else directory / candidate
        ).resolve()


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """A process-local config.toml-shaped override, shared by every command."""

    values: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", freeze_mapping(self.values))

    @classmethod
    def parse(cls, raw: str | None) -> RuntimeConfig:
        try:
            values = _object(json.loads(raw)) if raw is not None else {}
            if set(values) & {"origins", "defaults", "snapshots", "root", "home"}:
                raise ValueError
            _validate_values(values)
            runtime = cls(values)
            _workspace_values(runtime.values.get("workspaces", {}), None)
            return runtime
        except (ValueError, TypeError):
            raise ValueError("invalid runtime configuration patch") from None

    def apply(self, config: Mapping[str, object]) -> dict[str, object]:
        return _merge(config, self.values)

    def workspace_paths(self, key: str | None, *, directory: Path) -> dict[str, Path]:
        values = _workspace_values(self.values.get("workspaces", {}), key)
        return {
            name: (
                Path(value).expanduser()
                if Path(value).expanduser().is_absolute()
                else directory / Path(value).expanduser()
            ).resolve()
            for name, value in values.items()
            if name != "lab"
        }


def _workspace_values(value: object, key: str | None) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("workspaces config must be a table")
    values = cast(Mapping[str, object], value)
    keyed = any(isinstance(item, Mapping) for item in values.values())
    tables = values.values() if keyed else (values,)
    for table in tables:
        if not isinstance(table, Mapping):
            raise ValueError("invalid runtime workspace path")
        for name, path in table.items():
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is None
                or not isinstance(path, str)
                or not path
            ):
                raise ValueError("invalid runtime workspace path")
    selected = (values.get(key, {}) if key is not None else {}) if keyed else values
    return cast(Mapping[str, str], selected)


def parse_source_metadata(data: object) -> ConfigOrigins:
    """Validate source identity independently of runtime configuration values."""
    try:
        values = _object(data)
        if set(values) - {"origins", "snapshots", "human", "state_revision"}:
            raise ValueError
        origins = {
            key: _absolute(value)
            for key, value in _object(values.get("origins", {})).items()
        }
        if any(
            key not in _SCOPES | _FILE_ORIGINS | {"toolang_root", "agent_root"}
            and not Path(key).is_absolute()
            for key in origins
        ):
            raise ValueError
        snapshots = _object(values.get("snapshots", {}))
        if set(snapshots) - {"config.toml"} or any(
            value is not None
            and (
                not isinstance(value, str)
                or re.fullmatch(r"[a-f0-9]{64}", value) is None
            )
            for value in snapshots.values()
        ):
            raise ValueError
        if "human" in values and not isinstance(values["human"], str):
            raise ValueError
        if "state_revision" in values and (
            not isinstance(values["state_revision"], str)
            or re.fullmatch(r"[a-f0-9]{64}", values["state_revision"]) is None
        ):
            raise ValueError
        return ConfigOrigins(origins, snapshots=cast(dict[str, str | None], snapshots))
    except (TypeError, ValueError):
        raise ValueError("invalid launch source metadata") from None


_CURRENT: ContextVar[RuntimeConfig | None] = ContextVar(
    "toolang_runtime_config", default=None
)


def current_runtime_config() -> RuntimeConfig | None:
    return _CURRENT.get()


@contextmanager
def runtime_config_scope(config: RuntimeConfig) -> Iterator[None]:
    token = _CURRENT.set(config)
    try:
        yield
    finally:
        _CURRENT.reset(token)
