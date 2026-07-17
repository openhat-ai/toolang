"""Pure plugin configuration parsing."""

from __future__ import annotations

from collections.abc import Mapping
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
