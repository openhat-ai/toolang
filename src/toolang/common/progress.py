"""Lightweight progress events for long-running CLI operations."""

from __future__ import annotations

from toolang.base.types.progress import (
    ProgressEvent,
    ProgressKind,
    ProgressSink,
    ProgressStage,
    ProgressStatus,
)


LAUNCH_PROGRESS_FILE_ENV = "TOOLANG_LAUNCH_PROGRESS_FILE"


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
        # Progress is advisory and must never change the owned operation's result.
        return


__all__ = ["LAUNCH_PROGRESS_FILE_ENV", "ProgressSink", "emit_progress"]
