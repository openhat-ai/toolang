"""Explicit launch-time configuration views and workspace bindings."""

from __future__ import annotations

from collections.abc import Mapping
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
_FILE_ORIGINS = {"root/catalog.json", "home/catalog.json"}
_STATE_FIELDS = {
    "psyches",
    "skills",
    "services",
    "prompts",
    "allow",
    "chores",
    "runnables",
}


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


@dataclass(frozen=True, slots=True)
class WorkspaceBinding:
    """Keep authorization identity separate from the mounted execution root."""

    source: Path
    path: Path
    temporary: bool = False

    def to_data(self) -> dict[str, object]:
        return {
            "source": str(self.source),
            "path": str(self.path),
            "temporary": self.temporary,
        }


def parse_workspace_bindings(raw: str) -> dict[str, WorkspaceBinding]:
    """Validate the common host/guest argv contract without exposing values."""
    try:
        values = json.loads(raw)
        if not isinstance(values, dict):
            raise ValueError
        result = {}
        for name, value in values.items():
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                or not isinstance(value, dict)
            ):
                raise ValueError
            if (
                set(value) != {"source", "path", "temporary"}
                or type(value["temporary"]) is not bool
            ):
                raise ValueError
            if name == "lab" and value["temporary"]:
                raise ValueError
            result[name] = WorkspaceBinding(
                _absolute(value["source"]), _absolute(value["path"]), value["temporary"]
            )
        return result
    except (ValueError, TypeError):
        raise ValueError("invalid workspace bindings startup input") from None


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


def _validate_workspace_targets(
    patch: object, bindings: Mapping[str, WorkspaceBinding]
) -> None:
    for name, value in _object(patch).items():
        if isinstance(value, dict):
            _validate_workspace_targets(value, bindings)
        else:
            binding = bindings.get(name)
            if binding is None or value != str(binding.path):
                raise ValueError


@dataclass(frozen=True, slots=True)
class ConfigOrigins:
    """Source identities and path resolution, without execution configuration."""

    origins: Mapping[str, Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "origins", MappingProxyType(dict(self.origins)))

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
        if self.origins:
            if value.startswith("~"):
                raise ValueError("uncaptured source path requires a new launch")
            return Path(reference)
        candidate = candidate.expanduser()
        return (
            candidate if candidate.is_absolute() else directory / candidate
        ).resolve()


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Immutable execution corrections with a separate source parsing context."""

    origins: Mapping[str, Path] = field(default_factory=dict)
    defaults: Mapping[str, Mapping[str, object]] = field(
        default_factory=dict, repr=False
    )
    overlays: Mapping[str, Mapping[str, object]] = field(
        default_factory=dict, repr=False
    )
    workspaces: Mapping[str, WorkspaceBinding] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("origins", "workspaces"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        for name in ("defaults", "overlays"):
            object.__setattr__(self, name, freeze_mapping(getattr(self, name)))

    @property
    def source_context(self) -> ConfigOrigins:
        return ConfigOrigins(self.origins)

    @classmethod
    def parse(
        cls,
        raw: str | None,
        *,
        workspaces: Mapping[str, WorkspaceBinding] | None = None,
    ) -> RuntimeConfig:
        try:
            data = _object(json.loads(raw)) if raw is not None else {}
            if set(data) - {"origins", "defaults", "root", "home"}:
                raise ValueError
            origins = {
                key: _absolute(value)
                for key, value in _object(data.get("origins", {})).items()
            }
            if any(
                key not in _SCOPES | _FILE_ORIGINS and not Path(key).is_absolute()
                for key in origins
            ):
                raise ValueError
            defaults = _object(data.get("defaults", {}))
            if set(defaults) - _SCOPES:
                raise ValueError
            overlays = {scope: data[scope] for scope in _SCOPES if scope in data}
            for scopes in (defaults, overlays):
                for scope in scopes:
                    values = _object(scopes[scope])
                    _validate_values(values)
                    if set(values) & _STATE_FIELDS or any(
                        key.startswith("__toolang_") for key in values
                    ):
                        raise ValueError
            for values in defaults.values():
                if "workspaces" in _object(values):
                    raise ValueError
            for values in overlays.values():
                if "workspaces" in _object(values):
                    _validate_workspace_targets(
                        _object(values)["workspaces"], workspaces or {}
                    )
            return cls(
                origins,
                cast(dict[str, dict[str, object]], defaults),
                cast(dict[str, dict[str, object]], overlays),
                workspaces or {},
            )
        except (ValueError, TypeError):
            raise ValueError("invalid runtime configuration patch") from None

    def apply(self, scope: str, config: Mapping[str, object]) -> dict[str, object]:
        """Derive a fresh view; never restore a revoked workspace from a patch."""
        result = _merge(self.defaults.get(scope, {}), config)
        overlay = mutable_data(self.overlays.get(scope, {}))
        if "workspaces" in overlay:
            overlay["workspaces"] = self._workspace_overlay(
                result.get("workspaces", {}), overlay["workspaces"]
            )
        return _merge(result, overlay)

    def _workspace_overlay(self, original: object, patch: object) -> dict[str, object]:
        source = _object(original)
        result: dict[str, object] = {}
        for name, target in _object(patch).items():
            if isinstance(target, dict):
                result[name] = self._workspace_overlay(source.get(name, {}), target)
                continue
            binding = self.workspaces.get(name)
            if binding is None or source.get(name) != str(binding.source):
                continue
            if target != str(binding.path):
                raise ValueError(
                    "runtime workspace correction conflicts with captured binding"
                )
            result[name] = target
        return result
