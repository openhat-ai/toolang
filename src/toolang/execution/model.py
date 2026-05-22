"""Compatibility exports for model selector resolution."""

from __future__ import annotations

from toolang.models.resolution import (
    DEFAULT_MODEL_SELECTOR,
    SupportsModelSelection,
    resolve_model,
    select_model_selectors,
    selectable_model_targets,
)

__all__ = (
    "DEFAULT_MODEL_SELECTOR",
    "SupportsModelSelection",
    "resolve_model",
    "select_model_selectors",
    "selectable_model_targets",
)
