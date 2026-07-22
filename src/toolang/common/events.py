"""Package-neutral event types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ProgressStatus = Literal["pending", "running", "ok", "failed", "skipped"]


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One progress update for a stable CLI item."""

    id: str
    phase: str
    label: str
    status: ProgressStatus
    detail: str | None = None
