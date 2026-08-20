"""Terminal-independent execution progress vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProgressTone = Literal["progress", "normal", "active", "error", "warning"]


@dataclass(frozen=True, slots=True)
class ProgressRow:
    """One semantic progress row before surface-specific styling."""

    text: str
    tone: ProgressTone = "progress"
    wrap_live: bool = False


@dataclass(frozen=True, slots=True)
class ProgressBlock:
    """One finalized or atomically replaceable group of progress rows."""

    key: str
    rows: tuple[ProgressRow, ...]


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    """Newly finalized blocks plus the complete current live snapshot."""

    finalized: tuple[ProgressBlock, ...] = ()
    live: tuple[ProgressBlock, ...] = ()
