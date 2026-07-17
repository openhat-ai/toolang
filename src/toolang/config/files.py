"""Root and agent configuration file resolution."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from .toml import load_optional_toml


def load_config_layers(
    toolang_root: Path,
    agent_name: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """Load root and agent config documents in override order."""

    return (
        load_optional_toml(toolang_root / "config.toml"),
        load_optional_toml(toolang_root / "agents" / agent_name / "config.toml"),
    )


def load_named_config(
    toolang_root: Path,
    agent_name: str,
    *,
    section: str,
    environ: Mapping[str, str],
) -> dict[str, dict[str, object]]:
    """Load and merge one table of named, environment-aware configs."""

    merged: dict[str, dict[str, object]] = {}
    for payload in load_config_layers(toolang_root, agent_name):
        table = payload.get(section)
        if not isinstance(table, dict):
            continue
        for name, value in table.items():
            if not isinstance(name, str) or not isinstance(value, dict):
                continue
            current = dict(merged.get(name, {}))
            current.update(
                resolve_env_refs(
                    dict(cast(dict[str, object], value)),
                    environ,
                    context=f"{section}.{name}",
                )
            )
            merged[name] = current
    return merged


def load_sandbox_config(
    toolang_root: Path,
    agent_name: str,
    *,
    environ: Mapping[str, str],
) -> dict[str, object] | None:
    """Load one merged sandbox config as package-neutral data."""

    driver: str | None = None
    target: str | None = None
    config: dict[str, object] = {}
    for payload in load_config_layers(toolang_root, agent_name):
        raw_sandbox = payload.get("sandbox")
        if not isinstance(raw_sandbox, dict):
            continue
        sandbox = cast(dict[str, object], raw_sandbox)
        raw_driver = sandbox.get("driver")
        if not isinstance(raw_driver, str) or not raw_driver.strip():
            raw_driver = sandbox.get("plugin")
        if isinstance(raw_driver, str) and raw_driver.strip():
            driver = raw_driver.strip()
            raw_target = sandbox.get("target")
            target = (
                raw_target.strip()
                if isinstance(raw_target, str) and raw_target.strip()
                else None
            )
        raw_config = sandbox.get("config")
        if isinstance(raw_config, dict):
            config.update(
                resolve_env_refs(
                    dict(cast(dict[str, object], raw_config)),
                    environ,
                    context="sandbox.config",
                )
            )
    if driver is None:
        return None
    return {"driver": driver, "target": target, "config": config}


def resolve_env_refs(
    payload: dict[str, object],
    environ: Mapping[str, str],
    *,
    context: str,
) -> dict[str, object]:
    """Resolve `_env` references in one explicit configuration mapping."""

    resolved = dict(payload)
    for key, value in tuple(payload.items()):
        if not key.endswith("_env") or not isinstance(value, str):
            continue
        target_key = key[:-4]
        if target_key in resolved:
            resolved.pop(key, None)
            continue
        env_name = value.strip()
        if not env_name:
            resolved.pop(key, None)
            continue
        env_value = environ.get(env_name)
        if env_value is None:
            raise ValueError(
                f"missing environment variable {env_name} for {context}.{key}; "
                f"set {env_name} or provide {context}.{target_key} directly"
            )
        resolved[target_key] = env_value
        resolved.pop(key, None)
    return resolved
