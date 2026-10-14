"""Lightweight progress events for long-running CLI operations."""

from __future__ import annotations

from typing import Literal

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
    subject: Literal["agent", "cap", "state"] | None = None,
    ref: str | None = None,
    scope: str | None = None,
    cached: bool = False,
    complete: bool = False,
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
                subject=subject,
                ref=ref,
                scope=scope,
                cached=cached,
                complete=complete,
            )
        )
    except Exception:
        # Progress is advisory and must not change the owned operation's result.
        return


__all__ = ["ProgressSink", "emit_progress"]
