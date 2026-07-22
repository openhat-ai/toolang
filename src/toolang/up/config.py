"""Agent-process configuration parsing and resolution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast


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
