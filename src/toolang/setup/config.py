"""Setup-owned configuration loading and policy resolution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import tomllib
from typing import cast

from dotenv import dotenv_values
from toolang.base.types.model import ModelRequest
from toolang.base.types.policy import AgentCeiling, RunDefaults, RunLimits
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.common.query import (
    resolve_query_sentinels,
)
from toolang.plugin.models.collections import MODEL_SCHEMA
from toolang.plugin.toolsets.collections import TOOL_SCHEMA


_CAP_KIND_BY_FIELD = {
    "psyches": "psyche",
    "skills": "skill",
    "services": "service",
    "prompts": "prompt",
}
_ALLOW_FIELDS = frozenset({"models", "tools", *_CAP_KIND_BY_FIELD})
_DEFAULT_FIELDS = frozenset({"model", "runnable"})
_LIMIT_FIELDS = frozenset(
    {
        "agic_model_calls",
        "agic_tool_calls",
        "tokens",
        "cost",
        "time",
    }
)
_RUNNABLE_REF_RE = re.compile(r"(?:(?:agic|flow):)?[A-Za-z_][A-Za-z0-9_-]*")
_SETUP_PLUGIN_FAMILIES = frozenset({"model_catalog", "model_adapter", "toolset"})


def load_setup_config(layout: AgentLayout) -> dict[str, object]:
    """Load the root-scoped setup configuration."""

    return _load_toml(layout.root_config)


def load_agent_config(layout: AgentLayout) -> dict[str, object]:
    """Load the agent-scoped setup policy configuration."""

    return _load_toml(layout.config)


def load_setup_envs(layout: AgentLayout) -> dict[str, str]:
    """Load root and agent dotenv defaults below the process environment."""

    envs = load_setup_dotenvs(layout)
    envs.update(os.environ)
    return envs


def load_setup_dotenvs(layout: AgentLayout) -> dict[str, str]:
    """Load the merged root and agent dotenv values without process values."""

    envs = _load_dotenv(layout.root_env)
    envs.update(_load_dotenv(layout.env))
    return envs


def project_setup_config(config: Mapping[str, object]) -> dict[str, object]:
    """Return the semantic authored fields owned by installed Setup."""

    projected = {
        name: _mutable_value(config[name])
        for name in ("models", "default", "limit")
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


def resolve_setup_allow(
    configs: Sequence[Mapping[str, object]],
    *,
    overrides: Mapping[str, tuple[str, ...] | None] | None = None,
) -> AgentCeiling:
    """Resolve Setup-owned model/tool allow configuration and overrides."""

    fields: dict[str, tuple[str, ...]] = {}
    for config in configs:
        raw_allow = _table(config, "allow")
        if raw_allow is None:
            continue
        _reject_unknown(raw_allow, _ALLOW_FIELDS, "allow field")
        for name, value in raw_allow.items():
            normalized = _query_values(str(name), value)
            if normalized is None:
                fields.pop(str(name), None)
            else:
                fields[str(name)] = normalized
    resolved_overrides = overrides or {}
    _reject_unknown(resolved_overrides, _ALLOW_FIELDS, "allow field")
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
    _validate_setup_allow_syntax(ceiling)
    return ceiling


def resolve_run_defaults(
    configs: Sequence[Mapping[str, object]],
    *,
    overrides: Mapping[str, str | None] | None = None,
) -> RunDefaults:
    """Resolve layered ``[default]`` configuration and frozen overrides."""

    fields: dict[str, str | None] = {}
    for config in configs:
        raw_default = _table(config, "default")
        if raw_default is None:
            continue
        _reject_unknown(raw_default, _DEFAULT_FIELDS, "default field")
        fields.update(
            {
                str(name): _default_value(str(name), value)
                for name, value in raw_default.items()
            }
        )
    resolved_overrides = overrides or {}
    _reject_unknown(resolved_overrides, _DEFAULT_FIELDS, "default field")
    fields.update(resolved_overrides)
    defaults = RunDefaults(**fields)
    if defaults.model is not None:
        ModelRequest(defaults.model)
    if defaults.runnable is not None and not _RUNNABLE_REF_RE.fullmatch(
        defaults.runnable
    ):
        raise ValueError(f"invalid default runnable ref: {defaults.runnable!r}")
    return defaults


def resolve_run_limits(
    configs: Sequence[Mapping[str, object]],
    *,
    overrides: Mapping[str, int | Decimal | None] | None = None,
) -> RunLimits:
    """Resolve layered ``[limit]`` configuration and frozen overrides."""

    limits = RunLimits()
    for config in configs:
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
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise TypeError(f"allow {name} must be an array of queries")
    try:
        return resolve_query_sentinels(
            cast(Sequence[str], value),
            label=f"allow {name}",
        )
    except ToolangError as error:
        raise ValueError(str(error)) from error


def _validate_setup_allow_syntax(ceiling: AgentCeiling) -> None:
    for query in ceiling.models or ():
        MODEL_SCHEMA.parse(query)
    for query in ceiling.tools or ():
        TOOL_SCHEMA.parse(query)


def _default_value(name: str, value: object) -> str | None:
    if not isinstance(value, str):
        raise TypeError(f"default {name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"default {name} must not be empty")
    return None if normalized.lower() == "none" else normalized


def _load_toml(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


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


def _limit_value(name: str, value: object) -> int | Decimal | None:
    if isinstance(value, str) and value.lower() == "none":
        return None
    if name == "cost":
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            raise TypeError("run limit cost must be a decimal string or number")
        try:
            parsed = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("run limit cost must be a decimal or none") from exc
        if not parsed.is_finite() or parsed < 0:
            raise ValueError("run limit cost must be non-negative or none")
        return parsed
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"run limit {name} must be an integer or 'none'")
    if value < 0:
        raise ValueError(f"run limit {name} must be non-negative or none")
    return value
