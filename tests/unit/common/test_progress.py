from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from toolang.common.events import ProgressEvent
from toolang.common.progress import emit_progress


def test_emit_progress_is_optional() -> None:
    emit_progress(
        None,
        id="agent:eve:root",
        kind="prepare",
        stage="materialize",
        label="Prepare state",
        status="running",
    )


def test_emit_progress_sends_complete_immutable_event() -> None:
    events: list[ProgressEvent] = []

    emit_progress(
        events.append,
        id="cap:skill:skill/review",
        kind="prepare",
        stage="resolve",
        label="Resolve review",
        status="ok",
        detail="skill/review",
    )

    assert events == [
        ProgressEvent(
            id="cap:skill:skill/review",
            kind="prepare",
            stage="resolve",
            label="Resolve review",
            status="ok",
            detail="skill/review",
        )
    ]
    with pytest.raises(FrozenInstanceError):
        events[0].status = "failed"  # type: ignore[misc]


def test_emit_progress_ignores_sink_failures() -> None:
    def fail(_event: ProgressEvent) -> None:
        raise RuntimeError("sink failed")

    emit_progress(
        fail,
        id="agent:eve:root",
        kind="prepare",
        stage="materialize",
        label="Prepare state",
        status="failed",
    )


@pytest.mark.parametrize(
    ("kind", "stage"),
    [
        ("prepare", "resolve"),
        ("prepare", "fetch"),
        ("prepare", "materialize"),
        ("setup", "load"),
        ("setup", "discover"),
        ("runtime", "create"),
        ("runtime", "start"),
        ("runtime", "stop"),
        ("runtime", "destroy"),
    ],
)
def test_progress_event_accepts_documented_kind_stage_pairs(
    kind: str,
    stage: str,
) -> None:
    event = ProgressEvent(
        id="item:1",
        kind=kind,  # type: ignore[arg-type]
        stage=stage,  # type: ignore[arg-type]
        label="Working",
        status="running",
    )

    assert (event.kind, event.stage) == (kind, stage)


@pytest.mark.parametrize(
    ("kind", "stage"),
    [
        ("prepare", "load"),
        ("setup", "fetch"),
        ("runtime", "materialize"),
        ("unknown", "start"),
    ],
)
def test_progress_event_rejects_invalid_kind_stage_pairs(
    kind: str,
    stage: str,
) -> None:
    with pytest.raises(ValueError, match="invalid progress kind-stage pair"):
        ProgressEvent(
            id="item:1",
            kind=kind,  # type: ignore[arg-type]
            stage=stage,  # type: ignore[arg-type]
            label="Working",
            status="running",
        )
