"""Setup-owned configuration and environment loading."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import tomllib
from typing import cast

from dotenv import dotenv_values
from toolang.base.types.run import RunLimits
from toolang.common.layout import AgentLayout


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


def load_setup_envs(layout: AgentLayout) -> dict[str, str]:
    """Load root dotenv defaults below the process environment."""

    envs = _load_dotenv(layout.root_env)
    envs.update(os.environ)
    return envs


def load_run_limits(layout: AgentLayout) -> RunLimits:
    """Load root and agent run-limit defaults in precedence order."""

    return resolve_run_limits(
        (
            _load_toml(layout.root_config),
            _load_toml(layout.config),
        )
    )


def resolve_run_limits(configs: Sequence[Mapping[str, object]]) -> RunLimits:
    """Resolve layered ``[run.limits]`` configuration."""

    limits = RunLimits()
    for config in configs:
        run = config.get("run")
        if run is None:
            continue
        if not isinstance(run, Mapping):
            raise ValueError("run config must be a table")
        raw_limits = cast(Mapping[str, object], run).get("limits")
        if raw_limits is None:
            continue
        if not isinstance(raw_limits, Mapping):
            raise ValueError("run.limits config must be a table")
        unknown = sorted(str(name) for name in raw_limits if name not in _LIMIT_FIELDS)
        if unknown:
            raise ValueError(f"unknown run limit: {', '.join(unknown)}")
        values = {
            str(name): _config_limit_value(str(name), value)
            for name, value in raw_limits.items()
        }
        limits = replace(limits, **values)
    return limits


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


def _config_limit_value(name: str, value: object) -> int | Decimal | None:
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
