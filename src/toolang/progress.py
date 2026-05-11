"""Lightweight progress events for long-running CLI operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


ProgressStatus = Literal["running", "ok", "failed", "skipped"]
ProgressSink = Callable[["ProgressEvent"], None]


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One progress update for a stable CLI item."""

    id: str
    phase: str
    label: str
    status: ProgressStatus
    detail: str | None = None


def emit_progress(
    progress: ProgressSink | None,
    *,
    id: str,
    phase: str,
    label: str,
    status: ProgressStatus,
    detail: str | None = None,
) -> None:
    """Send one progress event when a sink is configured."""

    if progress is None:
        return
    progress(
        ProgressEvent(
            id=id,
            phase=phase,
            label=label,
            status=status,
            detail=detail,
        )
    )
