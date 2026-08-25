"""Pure plugin configuration parsing."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

PluginFamily = Literal[
    "toolset",
    "channel",
    "sandbox",
    "model_catalog",
    "model_adapter",
]

PLUGIN_FAMILIES: frozenset[str] = frozenset(
    {"toolset", "channel", "sandbox", "model_catalog", "model_adapter"}
)
_LEGACY_PLUGIN_SECTIONS = frozenset({"tools", "channels", "plugins"})
_MODEL_FIELDS = frozenset({"default", "providers", "aliases"})
_SANDBOX_FIELDS = frozenset({"driver", "target"})


@dataclass(frozen=True, slots=True)
class ChannelBinding:
    """One configured channel plugin binding."""

    name: str
    config: dict[str, object]


@dataclass(frozen=True, slots=True)
class SandboxBinding:
    """One configured sandbox selection."""

    name: str
    spec: str | None


def merge_plugin_configs(
    config_layers: Sequence[Mapping[str, object]],
    *,
    family: PluginFamily,
    environ: Mapping[str, str],
) -> dict[str, dict[str, object]]:
    """Merge one canonical plugin family from root and agent config layers."""

    merged: dict[str, dict[str, object]] = {}
    for payload in config_layers:
        validate_plugin_config(payload)
        raw_plugin = payload.get("plugin")
        if raw_plugin is None:
            continue
        plugin = cast(Mapping[str, object], raw_plugin)
        raw_family = plugin.get(family)
        if raw_family is None:
            continue
        family_table = cast(Mapping[str, object], raw_family)
        for raw_name, raw_value in family_table.items():
            name = str(raw_name).strip()
            value = cast(Mapping[str, object], raw_value)
            merged[name] = _merge_mappings(merged.get(name, {}), value)

    return {
        name: resolve_env_refs(
            config,
            environ,
            context=f"plugin.{family}.{name}",
        )
        for name, config in merged.items()
    }


def validate_plugin_config(payload: Mapping[str, object]) -> None:
    """Reject malformed canonical plugin config and removed legacy shapes."""

    legacy = sorted(
        section for section in _LEGACY_PLUGIN_SECTIONS if section in payload
    )
    if legacy:
        raise ValueError(f"unsupported plugin config section: {', '.join(legacy)}")

    raw_models = payload.get("models")
    if raw_models is not None:
        models = _require_table(raw_models, context="models")
        _reject_unknown(models, _MODEL_FIELDS, context="models")

    raw_sandbox = payload.get("sandbox")
    if raw_sandbox is not None:
        sandbox = _require_table(raw_sandbox, context="sandbox")
        _reject_unknown(sandbox, _SANDBOX_FIELDS, context="sandbox")

    raw_plugin = payload.get("plugin")
    if raw_plugin is None:
        return
    plugin = _require_table(raw_plugin, context="plugin")
    _reject_unknown(plugin, PLUGIN_FAMILIES, context="plugin")
    for raw_family, raw_family_table in plugin.items():
        family = str(raw_family)
        family_table = _require_table(
            raw_family_table,
            context=f"plugin.{family}",
        )
        for raw_name, raw_config in family_table.items():
            name = str(raw_name).strip()
            if not name:
                raise ValueError(f"plugin.{family} names must not be empty")
            _require_table(raw_config, context=f"plugin.{family}.{name}")


def resolve_env_refs(
    payload: Mapping[str, object],
    environ: Mapping[str, str],
    *,
    context: str,
) -> dict[str, object]:
    """Recursively resolve explicit ``_env`` references in one plugin mapping."""

    resolved: dict[str, object] = {}
    for raw_key, value in payload.items():
        key = str(raw_key)
        if key.endswith("_env"):
            target_key = key[:-4]
            if target_key in payload:
                continue
            if not isinstance(value, str):
                raise TypeError(f"{context}.{key} must name an environment variable")
            env_name = value.strip()
            if not env_name:
                continue
            env_value = environ.get(env_name)
            if env_value is None:
                raise ValueError(
                    f"missing environment variable {env_name} for {context}.{key}; "
                    f"set {env_name} or provide {context}.{target_key} directly"
                )
            resolved[target_key] = env_value
            continue
        resolved[key] = _resolve_env_value(
            value,
            environ,
            context=f"{context}.{key}",
        )
    return resolved


def parse_channel_bindings(
    configs: Mapping[str, Mapping[str, object]],
) -> dict[str, ChannelBinding]:
    """Parse channel configs whose names directly select their entry points."""

    return {
        name: ChannelBinding(name=name, config=dict(payload))
        for name, payload in configs.items()
    }


def resolve_sandbox_binding(
    config_layers: Sequence[Mapping[str, object]],
) -> SandboxBinding | None:
    """Resolve the canonical core ``[sandbox]`` selection."""

    driver: str | None = None
    target: str | None = None
    for payload in config_layers:
        validate_plugin_config(payload)
        raw_sandbox = payload.get("sandbox")
        if raw_sandbox is None:
            continue
        sandbox = cast(Mapping[str, object], raw_sandbox)
        if "driver" in sandbox:
            raw_driver = sandbox["driver"]
            if not isinstance(raw_driver, str) or not raw_driver.strip():
                raise ValueError("sandbox.driver must be a non-empty string")
            driver = raw_driver.strip()
            if "target" not in sandbox:
                target = None
        if "target" in sandbox:
            raw_target = sandbox["target"]
            if not isinstance(raw_target, str):
                raise TypeError("sandbox.target must be a string")
            target = raw_target.strip() or None
    if driver is None:
        return None
    return SandboxBinding(name=driver, spec=target)


def _resolve_env_value(
    value: object,
    environ: Mapping[str, str],
    *,
    context: str,
) -> object:
    if isinstance(value, Mapping):
        return resolve_env_refs(
            {str(key): item for key, item in value.items()},
            environ,
            context=context,
        )
    if isinstance(value, list):
        return [
            _resolve_env_value(item, environ, context=f"{context}[{index}]")
            for index, item in enumerate(value)
        ]
    return value


def _merge_mappings(
    base: Mapping[str, object],
    override: Mapping[str, object],
) -> dict[str, object]:
    merged = {str(key): _copy_value(value) for key, value in base.items()}
    for raw_key, value in override.items():
        key = str(raw_key)
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_mappings(
                cast(Mapping[str, object], current),
                cast(Mapping[str, object], value),
            )
        else:
            merged[key] = _copy_value(value)
    return merged


def _copy_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _copy_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_value(item) for item in value]
    return value


def _require_table(value: object, *, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{context} config must be a table")
    return cast(Mapping[str, object], value)


def _reject_unknown(
    values: Mapping[str, object],
    allowed: frozenset[str],
    *,
    context: str,
) -> None:
    unknown = sorted(str(name) for name in values if name not in allowed)
    if unknown:
        raise ValueError(f"unknown {context} config field: {', '.join(unknown)}")
