"""Models.dev catalog file selection and precedence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from dataclasses import dataclass
from typing import Literal

from toolang.common.layout import AgentLayout
from toolang.common.config_sources import (
    ConfigSource,
    config_sources,
    source_catalog,
    catalog_config,
)

MODEL_CATALOG_ENV = "TOOLANG_MODEL_CATALOG"
DEFAULT_MAX_CATALOG_BYTES = 32 * 1024 * 1024
PACKAGED_MODEL_CATALOG = Path(__file__).parent / "data" / "catalog.json"


@dataclass(frozen=True, slots=True)
class ModelCatalogSelection:
    """Retain how a catalog was selected independently of its resolved path."""

    path: Path
    scope: Literal["packaged", "root", "home", "custom"]


def resolve_model_catalog_path(
    layout: AgentLayout,
    *,
    explicit: Path | None = None,
    environ: Mapping[str, str] | None = None,
    include_agent: bool = True,
    sources: Sequence[ConfigSource] | None = None,
) -> Path:
    return resolve_model_catalog_selection(
        layout,
        explicit=explicit,
        environ=environ,
        include_agent=include_agent,
        sources=sources,
    ).path


def resolve_model_catalog_selection(
    layout: AgentLayout,
    *,
    explicit: Path | None = None,
    environ: Mapping[str, str] | None = None,
    include_agent: bool = True,
    sources: Sequence[ConfigSource] | None = None,
) -> ModelCatalogSelection:
    """Choose explicit/environment, nearest authored, then packaged inputs."""
    if explicit is not None:
        path = explicit.expanduser().resolve(strict=False)
        _require_catalog_candidate(path, label="explicit model catalog")
        return ModelCatalogSelection(path, "custom")
    configured = str((environ or {}).get(MODEL_CATALOG_ENV, "")).strip()
    if configured:
        path = Path(configured).expanduser().resolve(strict=False)
        _require_catalog_candidate(path, label=MODEL_CATALOG_ENV)
        return ModelCatalogSelection(path, "custom")
    if layout.runtime_config is not None and layout.runtime_config.origins:
        # Host selection already decided which files to mount. Authored origins
        # remain host identities and must never be opened in the guest.
        candidates = (
            *(((layout.agent_home / "catalog.json", "home"),) if include_agent else ()),
            (layout.agent_root / "catalog.json", "root"),
        )
        for path, scope in candidates:
            if f"{scope}/catalog.json" not in layout.runtime_config.origins:
                continue
            _require_catalog_candidate(path, label="mounted model catalog")
            return ModelCatalogSelection(path, scope)
    else:
        for source in reversed(
            sources
            if sources is not None
            else config_sources(layout, include_agent=include_agent)
        ):
            selected = source_catalog(
                (source,),
                roaming=bool(layout.project_inputs) and source.scope != "home",
            )
            if selected is not None:
                config = catalog_config(source.config)
                scope = (
                    "custom"
                    if config is not None and "path" in config
                    else "home"
                    if source.scope == "home" or source.path == layout.config
                    else "root"
                )
                return ModelCatalogSelection(selected.resolve(strict=False), scope)
    _require_catalog_candidate(PACKAGED_MODEL_CATALOG, label="packaged model catalog")
    return ModelCatalogSelection(
        PACKAGED_MODEL_CATALOG.resolve(strict=False), "packaged"
    )


def _require_catalog_candidate(path: Path, *, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
