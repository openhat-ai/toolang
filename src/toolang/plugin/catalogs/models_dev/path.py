"""Models.dev catalog file selection and precedence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from dataclasses import dataclass, field
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
    _not_found_label: str | None = field(
        default=None, repr=False, compare=False, kw_only=True
    )

    def require_file(self) -> None:
        """Validate this captured choice without resolving source inputs again."""
        if self.path.is_file():
            return
        if self._not_found_label is None:
            raise ValueError(f"model catalog is not a file: {self.path}")
        raise FileNotFoundError(f"{self._not_found_label} not found: {self.path}")


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
    selected = _select_model_catalog(
        layout,
        explicit=explicit,
        environ=environ,
        include_agent=include_agent,
        sources=sources,
    )
    if validate:
        selected.require_file()
    return selected


def _select_model_catalog(
    layout: AgentLayout,
    *,
    explicit: Path | None,
    environ: Mapping[str, str] | None,
    include_agent: bool,
    sources: Sequence[ConfigSource] | None,
) -> ModelCatalogSelection:
    if explicit is not None:
        path = explicit.expanduser().resolve(strict=False)
        return ModelCatalogSelection(
            path, "custom", _not_found_label="explicit model catalog"
        )
    configured = str((environ or {}).get(MODEL_CATALOG_ENV, "")).strip()
    if configured:
        path = Path(configured).expanduser().resolve(strict=False)
        return ModelCatalogSelection(path, "custom", _not_found_label=MODEL_CATALOG_ENV)
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
            return ModelCatalogSelection(
                local, scope, _not_found_label="placement model catalog"
            )
        config_path = layout.config if scope == "home" else layout.root_config
        selected = source_catalog(
            (ConfigSource(config_path, source.config, source.scope),),
            roaming=False,
            validate=False,
        )
        if selected is not None:
            selected = layout.local_source_path(selected).resolve(strict=False)
            owned = selected.is_relative_to(
                layout.root_config.parent
            ) or selected.is_relative_to(layout.agent_home)
            return ModelCatalogSelection(
                selected,
                "custom" if declared and not owned else scope,
            )
    return ModelCatalogSelection(
        PACKAGED_MODEL_CATALOG.resolve(strict=False),
        "packaged",
        _not_found_label="packaged model catalog",
    )
