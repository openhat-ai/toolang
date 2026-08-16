"""Setup-owned configuration loading and policy resolution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import tomllib
from typing import cast

from dotenv import dotenv_values
from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits
from toolang.common.layout import AgentLayout
from toolang.common.selectors import parse_selector


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
    """Load root dotenv defaults below the process environment."""

    envs = _load_dotenv(layout.root_env)
    envs.update(os.environ)
    return envs


def resolve_agent_ceiling(
    configs: Sequence[Mapping[str, object]],
    *,
    overrides: Mapping[str, tuple[str, ...] | None] | None = None,
) -> AgentCeiling:
    """Resolve layered ``[allow]`` configuration and frozen overrides."""

    fields: dict[str, tuple[str, ...]] = {}
    for config in configs:
        raw_allow = _table(config, "allow")
        if raw_allow is None:
            continue
        _reject_unknown(raw_allow, _ALLOW_FIELDS, "allow field")
        for name, value in raw_allow.items():
            fields[str(name)] = _selector_values(str(name), value)
    resolved_overrides = overrides or {}
    _reject_unknown(resolved_overrides, _ALLOW_FIELDS, "allow field")
    for name, value in resolved_overrides.items():
        if value is None:
            fields.pop(name, None)
        else:
            fields[name] = tuple(value)

    caps_present = any(name in fields for name in {"caps", *_CAP_KIND_BY_FIELD})
    cap_selectors: list[str] = list(fields.get("caps", ()))
    for plural, kind in _CAP_KIND_BY_FIELD.items():
        cap_selectors.extend(
            _cap_kind_selector(kind, selector) for selector in fields.get(plural, ())
        )
    ceiling = AgentCeiling(
        models=fields.get("models"),
        tools=fields.get("tools"),
        caps=tuple(cap_selectors) if caps_present else None,
    )
    _validate_agent_ceiling_syntax(ceiling)
    return ceiling


def resolve_run_bindings(
    configs: Sequence[Mapping[str, object]],
    *,
    overrides: Mapping[str, str | None] | None = None,
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
        parse_selector(bindings.model, domain="model")
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


def _selector_values(name: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise TypeError(f"allow {name} must be an array of selectors")
    result: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise TypeError(f"allow {name} selectors must be strings")
        selector = raw.strip()
        if not selector:
            raise ValueError(f"allow {name} selectors must not be empty")
        if selector not in result:
            result.append(selector)
    return tuple(result)


def _cap_kind_selector(kind: str, value: str) -> str:
    parsed = parse_selector(value, domain="cap", implicit_family=kind)
    text = value.strip()
    suffix = text[text.find("[") :] if "[" in text else ""
    return f"{kind}/{parsed.pattern}{suffix}"


def _validate_agent_ceiling_syntax(ceiling: AgentCeiling) -> None:
    for selector in ceiling.models or ():
        parse_selector(selector, domain="model")
    for selector in ceiling.tools or ():
        parse_selector(selector, domain="tool")
    for selector in ceiling.caps or ():
        parse_selector(selector, domain="cap")


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
        for key, value in dotenv_values(path).items()
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
