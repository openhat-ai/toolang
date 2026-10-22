"""Shared execution progress semantics for CLI presentation surfaces."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .projector import ProgressProjector as ProgressProjector
    from .state import Metrics as Metrics
    from .types import (
        ProgressBlock as ProgressBlock,
        ProgressFormat as ProgressFormat,
        ProgressRow as ProgressRow,
        ProgressSurface as ProgressSurface,
        ProgressTone as ProgressTone,
        ProgressUpdate as ProgressUpdate,
    )

_EXPORT_MODULES = {
    "Metrics": ".state",
    "ProgressBlock": ".types",
    "ProgressFormat": ".types",
    "ProgressRow": ".types",
    "ProgressSurface": ".types",
    "ProgressTone": ".types",
    "ProgressUpdate": ".types",
    "ProgressProjector": ".projector",
}


def __getattr__(name: str) -> Any:
    try:
        module = _EXPORT_MODULES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


__all__ = list(_EXPORT_MODULES)
