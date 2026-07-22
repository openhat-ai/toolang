"""Lightweight progress events for long-running CLI operations."""

from __future__ import annotations

from collections.abc import Callable

from .events import ProgressEvent, ProgressStatus

ProgressSink = Callable[[ProgressEvent], None]


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
