"""Setup-owned configuration loading and policy resolution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import tomllib
from typing import Any, cast

from dotenv import dotenv_values
from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits
from toolang.common.errors import ToolangError
from toolang.common.layout import AgentLayout
from toolang.common.query import (
    CollectionSchema,
    prefix_query_value,
    resolve_query_sentinels,
)
from toolang.plugin.models.resolution import RUNTIME_MODEL_SCHEMA
from toolang.plugin.toolsets.collections import TOOL_SCHEMA


_CAP_KIND_BY_FIELD = {
    "psyches": "psyche",
    "skills": "skill",
    "services": "service",
    "prompts": "prompt",
}
_ALLOW_FIELDS = frozenset({"models", "tools", "caps", *_CAP_KIND_BY_FIELD})
_BINDING_FIELDS = frozenset({"model", "runnable"})
_LIMIT_FIELDS = frozenset(
    {
        "agic_model_calls",
        "agic_tool_calls",
        "tokens",
        "cost",
        "time",
    }
)


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


def resolve_agent_ceiling(
    configs: Sequence[Mapping[str, object]],
    *,
    overrides: Mapping[str, tuple[str, ...] | None] | None = None,
    cap_query_schema: CollectionSchema[Any] | None = None,
) -> AgentCeiling:
    """Resolve layered ``[allow]`` configuration and frozen overrides."""

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

    caps_present = any(name in fields for name in {"caps", *_CAP_KIND_BY_FIELD})
    cap_queries: list[str] = list(fields.get("caps", ()))
    for plural, kind in _CAP_KIND_BY_FIELD.items():
        cap_queries.extend(
            _cap_kind_query(kind, query) for query in fields.get(plural, ())
        )
    ceiling = AgentCeiling(
        models=fields.get("models"),
        tools=fields.get("tools"),
        caps=tuple(cap_queries) if caps_present else None,
    )
    _validate_agent_ceiling_syntax(ceiling, cap_query_schema=cap_query_schema)
    return ceiling


def resolve_run_bindings(
    configs: Sequence[Mapping[str, object]],
    *,
    overrides: Mapping[str, str | None] | None = None,
    runnable_query_schema: CollectionSchema[Any] | None = None,
) -> RunBindings:
    """Resolve layered ``[default]`` configuration and frozen overrides."""

    fields: dict[str, str | None] = {}
    for config in configs:
        raw_default = _table(config, "default")
        if raw_default is None:
            continue
        _reject_unknown(raw_default, _BINDING_FIELDS, "default field")
        fields.update(
            {
                str(name): _binding_value(str(name), value)
                for name, value in raw_default.items()
            }
        )
    resolved_overrides = overrides or {}
    _reject_unknown(resolved_overrides, _BINDING_FIELDS, "default field")
    fields.update(resolved_overrides)
    bindings = RunBindings(**fields)
    if bindings.model is not None:
        RUNTIME_MODEL_SCHEMA.parse(bindings.model)
    if bindings.runnable is not None and runnable_query_schema is not None:
        runnable_query_schema.parse(bindings.runnable)
    return bindings


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


def _cap_kind_query(kind: str, value: str) -> str:
    return prefix_query_value(value, prefix=kind, separator="/")


def _validate_agent_ceiling_syntax(
    ceiling: AgentCeiling,
    *,
    cap_query_schema: CollectionSchema[Any] | None,
) -> None:
    for query in ceiling.models or ():
        RUNTIME_MODEL_SCHEMA.parse(query)
    for query in ceiling.tools or ():
        TOOL_SCHEMA.parse(query)
    if cap_query_schema is not None:
        for query in ceiling.caps or ():
            cap_query_schema.parse(query)


def _binding_value(name: str, value: object) -> str | None:
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
