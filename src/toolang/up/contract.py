"""Secret-free comparison of explicitly requested executor settings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from toolang.plugin.catalogs.models_dev.path import MODEL_CATALOG_ENV
from .instance import read_instance
from .types import ALL_DRIVERS, ExecutionDrivers

if TYPE_CHECKING:
    from toolang.common.layout import AgentLayout
    from .sandbox import LaunchSpec


def _value(value):
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _value(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_value(item) for item in value)
    if isinstance(value, (tuple, list)):
        return [_value(item) for item in value]
    if isinstance(value, (Path, Enum)):
        return str(value)
    return value


def startup_contract(spec: LaunchSpec) -> dict[str, str]:
    bindings = {
        name: getattr(spec.serve, name)
        for name in (
            "host",
            "endpoint_host",
            "port",
            "ceiling_overrides",
            "default_overrides",
            "limit_overrides",
            "compact_override",
            "workspace_additions",
        )
    }
    bindings.update(
        model_catalog={**spec.dotenv_envs, **spec.environ}.get(MODEL_CATALOG_ENV),
    )
    bindings.update(development_bindings(spec.dev_artifact))
    saved = {key: _digest(value) for key, value in bindings.items()}
    for key, value in bindings.items():
        if isinstance(value, Mapping):
            saved.update(
                {f"{key}.{name}": _digest(item) for name, item in value.items()}
            )
    return saved


def development_bindings(artifact: Path | None) -> dict[str, object]:
    """Bind a resolved development wheel to its path and contents."""
    bindings: dict[str, object] = {
        "development": str(artifact) if artifact is not None else None
    }
    if artifact is not None:
        bindings["development_digest"] = sha256(artifact.read_bytes()).hexdigest()
    return bindings


def _digest(value: object) -> str:
    return sha256(json.dumps(_value(value), sort_keys=True).encode()).hexdigest()


def require_compatible(
    layout: AgentLayout,
    requested: Mapping[str, object],
    *additional: Mapping[str, object],
) -> None:
    """Compare only supplied requirements; omission retains the running binding."""
    requirements = (requested, *additional)
    if not any(requirements):
        return
    entry = read_instance(layout.runtime_status) or {}
    saved = entry.get("control", {}).get("startup_contract")
    if not isinstance(saved, dict):
        raise ValueError(
            "running startup parameters are unverifiable; stop and restart the agent"
        )
    different = sorted(
        {
            key
            for requirement in requirements
            for key, value in requirement.items()
            if saved.get(key) != _digest(value)
            and not (
                isinstance(value, Mapping)
                and all(
                    saved.get(f"{key}.{name}") == _digest(item)
                    for name, item in value.items()
                )
            )
        }
    )
    if different:
        labels = {
            "endpoint_host": "endpoint host",
            "ceiling_overrides": "allowed resources",
            "default_overrides": "run defaults",
            "limit_overrides": "run limits",
            "compact_override": "compaction model",
            "workspace_additions": "workspace bindings",
            "model_catalog": "model catalog",
            "development": "development wheel",
            "development_digest": "development wheel contents",
        }
        raise ValueError(
            "startup parameters differ: "
            + ", ".join(labels.get(key, key) for key in different)
            + "; stop and restart the agent to change these settings"
        )


def bound_drivers(layout: AgentLayout) -> ExecutionDrivers:
    """An omitted selection retains the live owner's verified driver binding."""
    entry = read_instance(layout.runtime_status) or {}
    raw = entry.get("control", {}).get("drivers")
    if not isinstance(raw, list) or any(
        not isinstance(name, str) or name not in ALL_DRIVERS for name in raw
    ):
        raise ValueError(
            "running driver bindings are unverifiable; stop and restart the agent"
        )
    return cast(ExecutionDrivers, frozenset(raw))
