"""Setup-owned configuration loading and policy resolution."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Iterator, cast

from dotenv import dotenv_values

from toolang.base.model_settings import apply_model_override, parse_model_body
from toolang.base.money import normalize_cost
from toolang.base.types.model import (
    ModelEffort,
    ModelMaxOutput,
    ModelOverride,
    ModelRequest,
)
from toolang.base.types.policy import AgentCeiling, RunDefaults, RunLimits
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.common.config_sources import read_config
from toolang.common.policy import resolve_query_sentinels

from .types import CompactConfig, TalkConfig

_CAP_KIND_BY_FIELD = {
    "psyches": "psyche",
    "skills": "skill",
    "services": "service",
    "prompts": "prompt",
}
_SETUP_ALLOW_FIELDS = frozenset({"models", "tools"})
_KNOWN_ALLOW_FIELDS = frozenset({*_SETUP_ALLOW_FIELDS, *_CAP_KIND_BY_FIELD})
_DEFAULT_FIELDS = frozenset({"model"})
_LIMIT_FIELDS = frozenset(
    {
        "agic_model_calls",
        "agic_tool_calls",
        "tokens",
        "cost",
        "time",
    }
)
_SETUP_PLUGIN_FAMILIES = frozenset({"model_catalog", "model_adapter", "toolset"})


def load_setup_config(layout: AgentLayout) -> dict[str, object]:
    """Load the root-scoped setup configuration."""

    return (
        {}
        if layout.project_inputs
        else read_config(layout.place_config, include_workspaces=False)
    )


def load_agent_config(layout: AgentLayout) -> dict[str, object]:
    """Load the agent-scoped setup policy configuration."""

    return read_config(layout.config)


def load_setup_envs(layout: AgentLayout) -> dict[str, str]:
    """Load place and home dotenv defaults below the process environment."""

    envs = load_setup_dotenvs(layout)
    envs.update(os.environ)
    return envs


def load_place_setup_envs(layout: AgentLayout) -> dict[str, str]:
    """Load place dotenv defaults below the process environment."""

    envs = _load_dotenv(layout.place_env)
    envs.update(os.environ)
    return envs


def load_setup_dotenvs(layout: AgentLayout) -> dict[str, str]:
    """Load the merged place and home dotenv values without process values."""

    if layout.project_inputs:
        return {}
    envs = _load_dotenv(layout.place_env)
    envs.update(_load_dotenv(layout.env))
    return envs


def project_setup_config(config: Mapping[str, object]) -> dict[str, object]:
    """Return the semantic authored fields owned by installed Setup."""

    projected = {
        name: _mutable_value(config[name])
        for name in ("models", "model", "compact", "talk", "limit")
        if name in config
    }
    raw_allow = config.get("allow")
    if isinstance(raw_allow, Mapping):
        allow_mapping = cast(Mapping[str, object], raw_allow)
        allow = {
            name: _mutable_value(allow_mapping[name])
            for name in ("models", "tools")
            if name in allow_mapping
        }
        if allow:
            projected["allow"] = allow
    elif raw_allow is not None:
        projected["allow"] = raw_allow
    raw_plugin = config.get("plugin")
    if isinstance(raw_plugin, Mapping):
        plugin_mapping = cast(Mapping[str, object], raw_plugin)
        plugin = {
            str(name): _mutable_value(value)
            for name, value in plugin_mapping.items()
            if name in _SETUP_PLUGIN_FAMILIES
        }
        if plugin:
            projected["plugin"] = plugin
    elif raw_plugin is not None:
        projected["plugin"] = raw_plugin
    return projected


def project_model_setup_config(config: Mapping[str, object]) -> dict[str, object]:
    """Return only authored fields that affect a derived model context."""

    projected = {
        name: _mutable_value(config[name]) for name in ("models",) if name in config
    }
    raw_plugin = config.get("plugin")
    if isinstance(raw_plugin, Mapping):
        plugin_mapping = cast(Mapping[str, object], raw_plugin)
        plugin = {
            str(name): _mutable_value(value)
            for name, value in plugin_mapping.items()
            if name in {"model_catalog", "model_adapter"}
        }
        catalogs = plugin.get("model_catalog")
        if isinstance(catalogs, dict):
            catalog_mapping = cast(dict[str, object], catalogs)
            models_dev = catalog_mapping.get("models_dev")
            if isinstance(models_dev, dict):
                models_dev_mapping = cast(dict[str, object], models_dev)
                models_dev_mapping.pop("path", None)
                if not models_dev_mapping:
                    catalog_mapping.pop("models_dev")
            if not catalog_mapping:
                plugin.pop("model_catalog")
        if plugin:
            projected["plugin"] = plugin
    elif raw_plugin is not None:
        projected["plugin"] = raw_plugin
    return projected


def resolve_setup_allow(
    configs: Sequence[Mapping[str, object]],
    *,
    overrides: Mapping[str, tuple[str, ...] | None] | None = None,
) -> AgentCeiling:
    """Resolve Setup-owned model/tool allow configuration and overrides."""

    fields = _resolve_setup_queries(configs, names=("models", "tools"))
    resolved_overrides = overrides or {}
    _reject_unknown(resolved_overrides, _SETUP_ALLOW_FIELDS, "Setup allow override")
    for name, value in resolved_overrides.items():
        normalized = None if value is None else _query_values(name, value)
        if normalized is None:
            fields.pop(name, None)
        else:
            fields[name] = normalized

    ceiling = AgentCeiling(
        models=fields.get("models"),
        tools=fields.get("tools"),
    )
    return ceiling


@contextmanager
def _origin(origins: Sequence[Path], index: int) -> Iterator[None]:
    try:
        yield
    except (TypeError, ValueError) as exc:
        if index < len(origins):
            raise type(exc)(f"{origins[index]}: {exc}") from exc
        raise


def _model_table(value: object, label: str) -> ModelOverride | None:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a table")
    table = cast(Mapping[str, object], value)
    _reject_unknown(table, frozenset({"ref", "effort", "max_output"}), f"{label} field")
    if not table:
        return None
    if any(value is None for value in table.values()):
        raise TypeError(f"{label} values cannot be null")
    if "effort" in table and not isinstance(table["effort"], (str, int)):
        raise TypeError(f"{label}.effort must be a level, token budget, or auto")
    if "ref" in table and not isinstance(table["ref"], str):
        raise TypeError(f"{label}.ref must be a string")
    try:
        return ModelOverride(
            identity=cast(str | None, table.get("ref")),
            effort=cast(ModelEffort | None, table.get("effort")),
            max_output=cast(ModelMaxOutput | None, table.get("max_output")),
        )
    except (TypeError, ValueError) as exc:
        raise type(exc)(f"{label}: {exc}") from exc


def resolve_compact_config(
    configs: Sequence[Mapping[str, object]],
    *,
    override: ModelOverride | None = None,
    origins: Sequence[Path] = (),
) -> CompactConfig:
    """Capture ordered model operations and independently inherited sizes."""
    result = CompactConfig()
    operations: list[ModelOverride] = []
    for index, config in enumerate(configs):
        with _origin(origins, index):
            table = _table(config, "compact")
            if table is None:
                continue
            _reject_unknown(
                table,
                frozenset({"model", "summary", "recent", "trigger"}),
                "compact field",
            )
            if isinstance(table.get("model"), Mapping):
                selected = _model_table(table["model"], "compact.model")
                if selected is not None:
                    if selected.identity == "unset":
                        raise ValueError(
                            "compact.model requires an exact model or an inherited model; unset is not supported"
                        )
                    operations.append(selected)
            for name in ("summary", "recent", "trigger"):
                if name in table:
                    result = replace(result, **{name: _compact_size(name, table[name])})
    if override is not None:
        _validate_compact_override(override)
        operations.append(override)
    if type(result.recent) is type(result.trigger) and result.recent >= result.trigger:
        raise ValueError("compact.recent must be less than compact.trigger")
    return replace(result, model=tuple(operations))


def _validate_compact_override(model: ModelOverride) -> None:
    if model.identity in (None, "default", "unset"):
        raise ValueError("compact.model requires an exact model")


def resolve_talk_config(
    configs: Sequence[Mapping[str, object]],
    *,
    limits: RunLimits,
    origins: Sequence[Path] = (),
) -> TalkConfig:
    """Capture Talk model operations above ordinary model and limit defaults."""
    operations: list[ModelOverride] = []
    for index, config in enumerate(configs):
        with _origin(origins, index):
            table = _table(config, "talk")
            if table is None:
                continue
            _reject_unknown(table, frozenset({"model", "limit"}), "talk field")
            if "model" in table:
                selected = _model_table(table["model"], "talk.model")
                if selected is not None:
                    operations.append(selected)
            if "limit" in table:
                try:
                    limits = resolve_run_limits(
                        ({"limit": table["limit"]},), base=limits
                    )
                except (TypeError, ValueError) as exc:
                    raise type(exc)(f"talk.limit: {exc}") from exc
    return TalkConfig(model=tuple(operations), limits=limits)


def _compact_size(name: str, value: object) -> int | float:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?%", value):
        fraction = float(Decimal(value[:-1]) / 100)
        if 0 < fraction <= 1:
            return fraction
    raise ValueError(
        f"compact.{name} requires a positive integer or percentage in (0%, 100%]"
    )


def resolve_tool_allow(
    configs: Sequence[Mapping[str, object]],
) -> tuple[str, ...] | None:
    """Resolve tool policy without parsing model-owned allow expressions."""
    return _resolve_setup_queries(configs, names=("tools",)).get("tools")


def _resolve_setup_queries(
    configs: Sequence[Mapping[str, object]], *, names: tuple[str, ...]
) -> dict[str, tuple[str, ...]]:
    fields: dict[str, tuple[str, ...]] = {}
    for config in configs:
        raw_allow = _table(config, "allow")
        if raw_allow is None:
            continue
        _reject_unknown(raw_allow, _KNOWN_ALLOW_FIELDS, "allow field")
        for name in names:
            if name not in raw_allow:
                continue
            normalized = _query_values(name, raw_allow[name])
            if normalized is None:
                fields.pop(name, None)
            else:
                fields[name] = normalized
    return fields


def resolve_run_defaults(
    configs: Sequence[Mapping[str, object]],
    *,
    overrides: Mapping[str, ModelOverride | str | None] | None = None,
    origins: Sequence[Path] = (),
) -> RunDefaults:
    """Resolve layered ``[model]`` configuration and frozen overrides."""

    model: ModelRequest | None = None
    for index, config in enumerate(configs):
        with _origin(origins, index):
            if "model" in config:
                selected = _model_table(config["model"], "model")
            else:
                selected = None
            model = apply_model_override(model, model, selected)
    resolved_overrides = overrides or {}
    _reject_unknown(resolved_overrides, _DEFAULT_FIELDS, "default field")
    if "model" in resolved_overrides:
        raw_model = resolved_overrides["model"]
        override = (
            raw_model
            if isinstance(raw_model, ModelOverride)
            else parse_model_body("unset" if raw_model is None else raw_model)
        )
        model = apply_model_override(model, model, override)
    return RunDefaults(model=model)


def resolve_run_limits(
    configs: Sequence[Mapping[str, object]],
    *,
    overrides: Mapping[str, int | float | None] | None = None,
    base: RunLimits | None = None,
    origins: Sequence[Path] = (),
) -> RunLimits:
    """Resolve layered ``[limit]`` configuration and frozen overrides."""

    limits = base if base is not None else RunLimits()
    for index, config in enumerate(configs):
        with _origin(origins, index):
            raw_limits = _table(config, "limit")
            if raw_limits is None:
                continue
            _reject_unknown(raw_limits, _LIMIT_FIELDS, "run limit")
            limits = replace(
                limits,
                **{
                    str(name): _limit_value(str(name), value)
                    for name, value in raw_limits.items()
                },
            )
    resolved_overrides = overrides or {}
    _reject_unknown(resolved_overrides, _LIMIT_FIELDS, "run limit")
    return replace(limits, **resolved_overrides)


def _table(
    config: Mapping[str, object],
    name: str,
) -> Mapping[str, object] | None:
    value = config.get(name)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} config must be a table")
    return cast(Mapping[str, object], value)


def _reject_unknown(
    values: Mapping[str, object],
    allowed: frozenset[str],
    label: str,
) -> None:
    unknown = sorted(str(name) for name in values if name not in allowed)
    if unknown:
        raise ValueError(f"unknown {label}: {', '.join(unknown)}")


def _query_values(name: str, value: object) -> tuple[str, ...] | None:
    if name == "models" and isinstance(value, str):
        value = (value,)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise TypeError(f"allow {name} must be an array of queries")
    try:
        return resolve_query_sentinels(
            cast(Sequence[str], value),
            label=f"allow {name}",
        )
    except ToolangError as error:
        raise ValueError(str(error)) from error


def _load_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return {
        key: value
        for key, value in dotenv_values(path, interpolate=False).items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _mutable_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _mutable_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_mutable_value(item) for item in value]
    return value


def _limit_value(name: str, value: object) -> int | float | None:
    if isinstance(value, str) and value.lower() == "none":
        return None
    if name == "cost":
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            raise TypeError("run limit cost must be a decimal string or number")
        try:
            parsed = float(str(value))
        except ValueError as exc:
            raise ValueError("run limit cost must be a decimal or none") from exc
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError("run limit cost must be non-negative or none")
        return normalize_cost(parsed)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"run limit {name} must be an integer or 'none'")
    if value < 0:
        raise ValueError(f"run limit {name} must be non-negative or none")
    return value
