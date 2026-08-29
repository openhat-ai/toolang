"""Lightweight progress events for long-running CLI operations."""

from __future__ import annotations

from toolang.base.types.progress import (
    ProgressEvent,
    ProgressKind,
    ProgressSink,
    ProgressStage,
    ProgressStatus,
)


def emit_progress(
    progress: ProgressSink | None,
    *,
    id: str,
    kind: ProgressKind,
    stage: ProgressStage,
    label: str,
    status: ProgressStatus,
    detail: str | None = None,
) -> None:
    """Send one advisory progress event when a sink is configured."""

    if progress is None:
        return
    try:
        progress(
            ProgressEvent(
                id=id,
                kind=kind,
                stage=stage,
                label=label,
                status=status,
                detail=detail,
            )
        )
    except Exception:
        # Progress is advisory and must not change the owned operation's result.
        return


__all__ = ["ProgressSink", "emit_progress"]
