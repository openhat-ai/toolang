from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from toolang.common.events import ProgressEvent
from toolang.common.progress import emit_progress


def test_emit_progress_is_optional() -> None:
    emit_progress(
        None,
        id="prepare.state",
        phase="prepare.state",
        label="Prepare state",
        status="running",
    )


def test_emit_progress_sends_complete_immutable_event() -> None:
    events: list[ProgressEvent] = []

    emit_progress(
        events.append,
        id="cap.resolve:skill/review",
        phase="cap.resolve",
        label="Resolve review",
        status="ok",
        detail="skill/review",
    )

    assert events == [
        ProgressEvent(
            id="cap.resolve:skill/review",
            phase="cap.resolve",
            label="Resolve review",
            status="ok",
            detail="skill/review",
        )
    ]
    with pytest.raises(FrozenInstanceError):
        events[0].status = "failed"  # type: ignore[misc]


def test_emit_progress_propagates_sink_failures() -> None:
    def fail(_event: ProgressEvent) -> None:
        raise RuntimeError("sink failed")

    with pytest.raises(RuntimeError, match="sink failed"):
        emit_progress(
            fail,
            id="prepare.state",
            phase="prepare.state",
            label="Prepare state",
            status="failed",
        )
