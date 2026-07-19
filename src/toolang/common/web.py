"""Web UI URL resolution shared by CLI and agent startup."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

DEFAULT_UI_BASE_URL = "https://too.run"


def resolve_ui_base_url(
    config: Mapping[str, object], *, environ: Mapping[str, str]
) -> str:
    """Resolve the WebUI base URL from explicit config and environment."""

    base = environ.get("TOOLANG_UI_BASE_URL", "").strip()
    if not base:
        base = environ.get("TOOLANG_WEBUI_BASE_URL", "").strip()
    web = config.get("web")
    if not base and isinstance(web, Mapping):
        configured = cast(Mapping[str, object], web).get("ui_base_url")
        base = configured.strip() if isinstance(configured, str) else ""
    return base or DEFAULT_UI_BASE_URL
