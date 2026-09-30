"""Models.dev catalog file selection and precedence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from toolang.common.layout import AgentLayout
from toolang.common.config_sources import ConfigSource, config_sources, source_catalog

MODEL_CATALOG_ENV = "TOOLANG_MODEL_CATALOG"
DEFAULT_MAX_CATALOG_BYTES = 32 * 1024 * 1024
PACKAGED_MODEL_CATALOG = Path(__file__).parent / "data" / "catalog.json"


def resolve_model_catalog_path(
    layout: AgentLayout,
    *,
    explicit: Path | None = None,
    environ: Mapping[str, str] | None = None,
    include_agent: bool = True,
    sources: Sequence[ConfigSource] | None = None,
) -> Path:
    """Resolve one complete catalog using explicit, home, root, package precedence."""

    if explicit is not None:
        path = explicit.expanduser().resolve(strict=False)
        _require_catalog_candidate(path, label="explicit model catalog")
        return path
    values = environ or {}
    configured = str(values.get(MODEL_CATALOG_ENV, "")).strip()
    if configured:
        path = Path(configured).expanduser().resolve(strict=False)
        _require_catalog_candidate(path, label=MODEL_CATALOG_ENV)
        return path
    selected = source_catalog(
        sources
        if sources is not None
        else config_sources(layout, include_agent=include_agent),
        roaming=layout.placement == "roaming",
    )
    if selected is not None:
        return selected.resolve(strict=False)
    _require_catalog_candidate(PACKAGED_MODEL_CATALOG, label="packaged model catalog")
    return PACKAGED_MODEL_CATALOG.resolve(strict=False)


def _require_catalog_candidate(path: Path, *, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
