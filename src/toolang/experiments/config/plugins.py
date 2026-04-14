"""Plugin config loading helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import cast

from ..base.types.sandbox import SandboxSelector


@dataclass(frozen=True, slots=True)
class ChannelBinding:
    """One configured channel binding loaded from config.toml."""

    name: str
    plugin: str
    config: dict[str, object]


@dataclass(frozen=True, slots=True)
class SandboxBinding:
    """One configured sandbox plugin binding."""

    selector: SandboxSelector
    config: dict[str, object]


def load_tool_plugin_config(
    toolang_root: Path,
    agent_name: str,
    *,
    environ: Mapping[str, str],
) -> dict[str, dict[str, object]]:
    """Load merged tool plugin config from root and agent config files."""

    return _load_plugin_table(
        toolang_root,
        agent_name,
        section="tools",
        environ=environ,
    )


def load_channel_bindings(
    toolang_root: Path,
    agent_name: str,
    *,
    environ: Mapping[str, str],
) -> dict[str, ChannelBinding]:
    """Load merged channel bindings from root and agent config files."""

    raw = _load_plugin_table(
        toolang_root,
        agent_name,
        section="channels",
        environ=environ,
    )
    bindings: dict[str, ChannelBinding] = {}
    for name, payload in raw.items():
        plugin = str(payload.get("plugin", "")).strip()
        if not plugin:
            raise ValueError(f"channel binding {name!r} is missing plugin")
        config = {
            key: value
            for key, value in payload.items()
            if key != "plugin"
        }
        bindings[name] = ChannelBinding(name=name, plugin=plugin, config=config)
    return bindings


def load_sandbox_binding(
    toolang_root: Path,
    agent_name: str,
    *,
    environ: Mapping[str, str],
) -> SandboxBinding | None:
    """Load one merged sandbox binding from root and agent config files."""

    selector: SandboxSelector | None = None
    config: dict[str, object] = {}
    for payload in _config_payloads(toolang_root, agent_name):
        raw_sandbox = payload.get("sandbox")
        if not isinstance(raw_sandbox, dict):
            continue
        sandbox = cast(dict[str, object], raw_sandbox)
        raw_driver = sandbox.get("driver")
        if not isinstance(raw_driver, str) or not raw_driver.strip():
            raw_driver = sandbox.get("plugin")
        if isinstance(raw_driver, str) and raw_driver.strip():
            raw_target = sandbox.get("target")
            target = raw_target.strip() if isinstance(raw_target, str) and raw_target.strip() else None
            selector = SandboxSelector(driver=raw_driver.strip(), target=target)
        raw_config = sandbox.get("config")
        if isinstance(raw_config, dict):
            config.update(
                _resolve_env_refs(
                    dict(cast(dict[str, object], raw_config)),
                    environ,
                )
            )
    if selector is None:
        return None
    return SandboxBinding(selector=selector, config=config)


def _load_plugin_table(
    toolang_root: Path,
    agent_name: str,
    *,
    section: str,
    environ: Mapping[str, str],
) -> dict[str, dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for payload in _config_payloads(toolang_root, agent_name):
        table = payload.get(section)
        if not isinstance(table, dict):
            continue
        for name, value in table.items():
            if not isinstance(name, str) or not isinstance(value, dict):
                continue
            current = dict(merged.get(name, {}))
            current.update(_resolve_env_refs(dict(cast(dict[str, object], value)), environ))
            merged[name] = current
    return merged


def _config_payloads(toolang_root: Path, agent_name: str) -> tuple[dict[str, object], dict[str, object]]:
    return (
        _load_toml(toolang_root / "config.toml"),
        _load_toml(toolang_root / "agents" / agent_name / "config.toml"),
    )


def _load_toml(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    return cast(dict[str, object], tomllib.loads(path.read_text(encoding="utf-8")))


def _resolve_env_refs(
    payload: dict[str, object],
    environ: Mapping[str, str],
) -> dict[str, object]:
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
            raise ValueError(f"missing environment variable for plugin config: {env_name}")
        resolved[target_key] = env_value
        resolved.pop(key, None)
    return resolved
