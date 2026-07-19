"""Pure plugin configuration parsing."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from toolang.base.types.sandbox import SandboxSelector


@dataclass(frozen=True, slots=True)
class ChannelBinding:
    """One configured channel binding."""

    name: str
    plugin: str
    config: dict[str, object]


@dataclass(frozen=True, slots=True)
class SandboxBinding:
    """One configured sandbox plugin binding."""

    selector: SandboxSelector
    config: dict[str, object]


def merge_named_configs(
    config_layers: Sequence[Mapping[str, object]],
    *,
    section: str,
    environ: Mapping[str, str],
) -> dict[str, dict[str, object]]:
    """Merge one named plugin section from explicit config layers."""

    merged: dict[str, dict[str, object]] = {}
    for payload in config_layers:
        table = payload.get(section)
        if not isinstance(table, Mapping):
            continue
        for raw_name, raw_value in table.items():
            if not isinstance(raw_name, str) or not isinstance(raw_value, Mapping):
                continue
            current = dict(merged.get(raw_name, {}))
            current.update(
                resolve_env_refs(
                    {str(key): value for key, value in raw_value.items()},
                    environ,
                    context=f"{section}.{raw_name}",
                )
            )
            merged[raw_name] = current
    return merged


def merge_sandbox_config(
    config_layers: Sequence[Mapping[str, object]],
    *,
    environ: Mapping[str, str],
) -> dict[str, object] | None:
    """Merge sandbox configuration from explicit root and home layers."""

    driver: str | None = None
    target: str | None = None
    config: dict[str, object] = {}
    for payload in config_layers:
        raw_sandbox = payload.get("sandbox")
        if not isinstance(raw_sandbox, Mapping):
            continue
        sandbox = cast(Mapping[str, object], raw_sandbox)
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
        if isinstance(raw_config, Mapping):
            config.update(
                resolve_env_refs(
                    {str(key): value for key, value in raw_config.items()},
                    environ,
                    context="sandbox.config",
                )
            )
    if driver is None:
        return None
    return {"driver": driver, "target": target, "config": config}


def resolve_env_refs(
    payload: Mapping[str, object],
    environ: Mapping[str, str],
    *,
    context: str,
) -> dict[str, object]:
    """Resolve explicit ``_env`` references in plugin configuration."""

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


def parse_channel_bindings(
    configs: Mapping[str, Mapping[str, object]],
) -> dict[str, ChannelBinding]:
    """Parse resolved channel plugin configurations."""

    bindings: dict[str, ChannelBinding] = {}
    for name, payload in configs.items():
        plugin = str(payload.get("plugin", "")).strip()
        if not plugin:
            raise ValueError(f"channel binding {name!r} is missing plugin")
        config = {key: value for key, value in payload.items() if key != "plugin"}
        bindings[name] = ChannelBinding(name=name, plugin=plugin, config=config)
    return bindings


def parse_sandbox_binding(
    payload: Mapping[str, object] | None,
) -> SandboxBinding | None:
    """Parse one resolved sandbox plugin configuration."""

    if payload is None:
        return None
    driver = payload.get("driver")
    if not isinstance(driver, str) or not driver.strip():
        raise ValueError("sandbox config is missing driver")
    raw_target = payload.get("target")
    target = (
        raw_target.strip()
        if isinstance(raw_target, str) and raw_target.strip()
        else None
    )
    raw_config = payload.get("config", {})
    config = (
        dict(cast(Mapping[str, object], raw_config))
        if isinstance(raw_config, Mapping)
        else {}
    )
    return SandboxBinding(
        selector=SandboxSelector(driver=driver.strip(), target=target),
        config=config,
    )
