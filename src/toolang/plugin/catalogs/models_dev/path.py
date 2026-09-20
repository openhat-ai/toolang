"""Models.dev catalog file selection and precedence."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from toolang.common.layout import AgentLayout

MODEL_CATALOG_ENV = "TOOLANG_MODEL_CATALOG"
DEFAULT_MAX_CATALOG_BYTES = 32 * 1024 * 1024
PACKAGED_MODEL_CATALOG = Path(__file__).parent / "data" / "catalog.json"

_CATALOG_FILENAME = "catalog.json"


def resolve_model_catalog_path(
    layout: AgentLayout,
    *,
    explicit: Path | None = None,
    environ: Mapping[str, str] | None = None,
    include_agent: bool = True,
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
    catalog_candidates = (
        (layout.home / _CATALOG_FILENAME, layout.root / _CATALOG_FILENAME)
        if include_agent
        else (layout.root / _CATALOG_FILENAME,)
    )
    for path in catalog_candidates:
        if path.is_file() or path.is_symlink():
            return path.resolve(strict=False)
    _require_catalog_candidate(PACKAGED_MODEL_CATALOG, label="packaged model catalog")
    return PACKAGED_MODEL_CATALOG.resolve(strict=False)


def _require_catalog_candidate(path: Path, *, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
