"""Web-facing config helpers."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .toml import load_optional_toml

DEFAULT_UI_BASE_URL = "https://too.run"


def resolve_ui_base_url(toolang_root: Path, *, environ: Mapping[str, str]) -> str:
    """Resolve the WebUI base URL from env or root config."""

    base = environ.get("TOOLANG_UI_BASE_URL", "").strip()
    if not base:
        base = environ.get("TOOLANG_WEBUI_BASE_URL", "").strip()
    if not base:
        configured = _load_root_web_config(toolang_root).get("ui_base_url")
        base = configured.strip() if isinstance(configured, str) else ""
    return base or DEFAULT_UI_BASE_URL


def resolve_cors_allowed_origins(
    toolang_root: Path,
    *,
    environ: Mapping[str, str],
) -> list[str] | None:
    """Resolve CORS origins from env or root config."""

    raw = environ.get("TOOLANG_CORS_ALLOWED_ORIGINS", "").strip()
    if not raw:
        raw = environ.get("TOOLANG_CORS_ORIGINS", "").strip()
    if raw:
        items = [item.strip() for item in raw.split(",") if item.strip()]
        return items or None
    configured = _load_root_web_config(toolang_root).get("cors_allowed_origins")
    if not isinstance(configured, list):
        return None
    items = [item.strip() for item in configured if isinstance(item, str) and item.strip()]
    return items or None


def _load_root_web_config(toolang_root: Path) -> dict[str, object]:
    payload = load_optional_toml(toolang_root / "config.toml")
    web = payload.get("web", {})
    if not isinstance(web, dict):
        return {}
    return {str(key): value for key, value in web.items()}
