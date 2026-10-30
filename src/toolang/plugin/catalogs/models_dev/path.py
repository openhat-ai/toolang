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
    config_origins,
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
    validate: bool = True,
) -> ModelCatalogSelection:
    """Choose inputs; optional validation also permits watching missing candidates."""
    if explicit is not None:
        path = explicit.expanduser().resolve(strict=False)
        if validate:
            _require_catalog_candidate(path, label="explicit model catalog")
        return ModelCatalogSelection(path, "custom")
    configured = str((environ or {}).get(MODEL_CATALOG_ENV, "")).strip()
    if configured:
        path = Path(configured).expanduser().resolve(strict=False)
        if validate:
            _require_catalog_candidate(path, label=MODEL_CATALOG_ENV)
        return ModelCatalogSelection(path, "custom")
    origins = (
        config_origins(layout).origins if layout.root_source_receipt.is_file() else {}
    )
    for source in reversed(
        sources
        if sources is not None
        else config_sources(layout, include_agent=include_agent)
    ):
        scope = "home" if source.scope == "home" else "root"
        local = (
            layout.agent_home / "catalog.json"
            if scope == "home"
            else layout.root_catalog
        )
        configured = catalog_config(source.config)
        declared = configured is not None and "path" in configured
        if (
            scope == "root"
            and "root/catalog.json" in origins
            and (local.exists() or local.is_symlink() or declared)
        ):
            if validate:
                _require_catalog_candidate(local, label="placement model catalog")
            return ModelCatalogSelection(local, scope)
        config_path = layout.config if scope == "home" else layout.root_config
        selected = source_catalog(
            (ConfigSource(config_path, source.config, source.scope),),
            roaming=False,
            validate=False,
        )
        if selected is not None:
            selected = layout.local_source_path(selected).resolve(strict=False)
            if validate and not selected.is_file():
                raise ValueError(f"model catalog is not a file: {selected}")
            owned = selected.is_relative_to(
                layout.root_config.parent
            ) or selected.is_relative_to(layout.agent_home)
            return ModelCatalogSelection(
                selected,
                "custom" if declared and not owned else scope,
            )
    if validate:
        _require_catalog_candidate(
            PACKAGED_MODEL_CATALOG, label="packaged model catalog"
        )
    return ModelCatalogSelection(
        PACKAGED_MODEL_CATALOG.resolve(strict=False), "packaged"
    )


def _require_catalog_candidate(path: Path, *, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
