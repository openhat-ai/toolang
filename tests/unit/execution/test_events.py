"""Canonical execution event codec tests."""

from __future__ import annotations

from typing import get_args

import pytest

from toolang.base.types.message import Message, TextDelta, TextPart
from toolang.execution.events import (
    PartBegin,
    PartDelta,
    PartEnd,
    RunBegin,
    RunEnd,
    RunEvent,
    StepBegin,
    StepEnd,
    run_event_from_data,
    run_event_to_data,
)
from toolang.execution.records import RunInputRef, StepOutputRef
from toolang.execution.types import (
    ControlStatus,
    RunStatus,
    StepErrorRef,
    StepPath,
    StepStatus,
)


_EVENTS: tuple[RunEvent, ...] = (
    RunBegin(
        run="run_root",
        input=RunInputRef(index=0),
        context={"root": "run_root"},
        started_at="2026-01-01T00:00:00Z",
    ),
    StepBegin(
        step=StepPath.parse("run_root/0"),
        kind="model",
        input=(
            RunInputRef(index=0),
            StepOutputRef(step=StepPath.parse("run_root/1")),
            Message.user("steer"),
        ),
        started_at="2026-01-01T00:00:01Z",
    ),
    PartBegin(step=StepPath.parse("run_root/0"), part=0, part_type="text"),
    PartDelta(step=StepPath.parse("run_root/0"), part=0, delta=TextDelta("hello")),
    PartEnd(step=StepPath.parse("run_root/0"), part=0, data=TextPart("hello")),
    StepEnd(
        step=StepPath.parse("run_root/0"),
        kind="model",
        status="succeeded",
        output=(TextPart("hello"),),
        finished_at="2026-01-01T00:00:02Z",
    ),
    RunEnd(
        run="run_root",
        status="succeeded",
        input=RunInputRef(index=0),
        output=StepOutputRef(step=StepPath.parse("run_root/0")),
        finished_at="2026-01-01T00:00:03Z",
    ),
)


def test_run_event_codec_round_trips_every_event_variant() -> None:
    for event in _EVENTS:
        assert run_event_from_data(run_event_to_data(event)) == event


def test_run_event_codec_rejects_unknown_discriminator() -> None:
    with pytest.raises(ValueError, match="unknown"):
        run_event_from_data(
            {
                "run": "run_root",
                "input": {"index": 0, "part": None},
                "type": "unknown",
            }
        )


def test_execution_status_vocabulary_has_canonical_order() -> None:
    assert get_args(RunStatus) == (
        "pending",
        "running",
        "succeeded",
        "failed",
        "canceled",
    )
    assert get_args(StepStatus) == (
        "pending",
        "running",
        "succeeded",
        "failed",
        "canceled",
    )
    assert get_args(ControlStatus) == (
        "pending",
        "applied",
        "wontapply",
        "revoked",
    )
    reserved = StepEnd(
        step=StepPath.parse("run_root/0"),
        kind="system",
        status="pending",
    )
    assert run_event_from_data(run_event_to_data(reserved)) == reserved


def test_run_event_codec_serializes_step_error_references() -> None:
    event = RunEnd(
        run="run_root",
        status="failed",
        error=StepErrorRef(StepPath.parse("run_root/2")),
    )

    data = run_event_to_data(event)

    assert data["error"] == {"step": "run_root/2"}
    assert run_event_from_data(data) == event
