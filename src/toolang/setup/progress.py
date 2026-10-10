"""Invocation-scoped progress for setup loading and discovery."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from toolang.common.progress import ProgressSink, emit_progress


@contextmanager
def setup_progress(
    progress: ProgressSink | None,
    *,
    target: str,
    resource: str,
    stage: Literal["load", "discover"] = "load",
) -> Iterator[None]:
    """Observe one blocking load without retaining its sink on a setup."""
    activity, completed, action = (
        ("Loading", "Loaded", "load")
        if stage == "load"
        else ("Discovering", "Discovered", "discover")
    )
    item_id = f"setup:{target}:{resource.replace(' ', '-')}"
    emit_progress(
        progress,
        id=item_id,
        kind="setup",
        stage=stage,
        label=f"{activity} {resource}...",
        status="running",
        detail=target,
    )
    try:
        yield
    except BaseException:
        emit_progress(
            progress,
            id=item_id,
            kind="setup",
            stage=stage,
            label=f"Failed to {action} {resource}",
            status="failed",
        )
        raise
    else:
        emit_progress(
            progress,
            id=item_id,
            kind="setup",
            stage=stage,
            label=f"{completed} {resource}",
            status="ok",
        )
