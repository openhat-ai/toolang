"""Lightweight progress events for long-running CLI operations."""

from __future__ import annotations

from toolang.base.types.progress import ProgressEvent, ProgressSink, ProgressStatus


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


__all__ = ["ProgressSink", "emit_progress"]
