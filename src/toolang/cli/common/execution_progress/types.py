"""Terminal-independent execution progress vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProgressTone = Literal["progress", "normal", "active", "error", "warning"]
ProgressFormat = Literal["plain", "markdown"]
ProgressSurface = Literal["none", "tool_summary", "tool_detail"]


@dataclass(frozen=True, slots=True)
class ProgressRow:
    """One semantic progress row before surface-specific styling."""

    text: str
    tone: ProgressTone = "progress"
    wrap_live: bool = False
    format: ProgressFormat = "plain"
    prefix: str = ""
    gap_before: bool = False
    surface: ProgressSurface = "none"


@dataclass(frozen=True, slots=True)
class ProgressBlock:
    """One committed fragment or atomically replaceable group of progress rows."""

    key: str
    rows: tuple[ProgressRow, ...]


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    """Newly committed fragments plus the complete current live snapshot."""

    committed: tuple[ProgressBlock, ...] = ()
    live: tuple[ProgressBlock, ...] = ()
