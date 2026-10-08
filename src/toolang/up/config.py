"""Agent-process configuration parsing and resolution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from toolang.common.config_sources import ConfigSource


def _port(value: object, source: str) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError(f"{source}: port must be an integer between 1 and 65535")
    return value


def agent_api_port(sources: Sequence[ConfigSource], *, home: Path) -> int | None:
    """Parse only home-scoped hosting settings, retaining their source errors."""
    result = None
    for source in sources:
        if "api" not in source.config:
            continue
        if source.path != home:
            raise ValueError(f"{source.path}: api requires agent home scope")
        raw = source.config["api"]
        if not isinstance(raw, dict) or set(raw) - {"port"}:
            raise ValueError(f"{source.path}: api only accepts port")
        raw = cast(dict[str, object], raw)
        if "port" in raw:
            result = _port(raw["port"], f"{source.path}: api.port")
    return result


def resolve_port_override(
    option: int | None,
    *,
    environ: Mapping[str, str],
    env_name: str,
    configured: int | None,
) -> int | None:
    """Resolve explicit inputs at the hosting boundary, before core startup."""
    if option is not None:
        return _port(option, "--port")
    if env_name in environ:
        raw = environ[env_name]
        if not raw.isascii() or not raw.isdecimal():
            raise ValueError(f"{env_name}: port must be an integer between 1 and 65535")
        return _port(int(raw), env_name)
    return _port(configured, "configured") if configured is not None else None


def resolve_cors_allowed_origins(
    config: Mapping[str, object], *, environ: Mapping[str, str]
) -> tuple[str, ...]:
    """Resolve API CORS origins from explicit config and environment."""

    raw = environ.get("TOOLANG_CORS_ALLOWED_ORIGINS", "").strip()
    if not raw:
        raw = environ.get("TOOLANG_CORS_ORIGINS", "").strip()
    if raw:
        return tuple(item.strip() for item in raw.split(",") if item.strip())
    web = config.get("web")
    configured = (
        cast(Mapping[str, object], web).get("cors_allowed_origins")
        if isinstance(web, Mapping)
        else None
    )
    if not isinstance(configured, Sequence) or isinstance(
        configured, (str, bytes, bytearray)
    ):
        return ()
    return tuple(
        item.strip() for item in configured if isinstance(item, str) and item.strip()
    )
