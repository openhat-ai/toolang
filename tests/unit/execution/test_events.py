"""Canonical execution event codec tests."""

from __future__ import annotations

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
from toolang.execution.records import OutputRef, RunControlRef


_EVENTS: tuple[RunEvent, ...] = (
    RunBegin(
        run="run_root",
        input=RunControlRef(index=0),
        context={"root": "run_root"},
        started_at="2026-01-01T00:00:00Z",
    ),
    StepBegin(
        step="run_root/0",
        kind="model",
        input=(
            RunControlRef(index=0),
            OutputRef(step="run_root/1"),
            Message.user("steer"),
        ),
        started_at="2026-01-01T00:00:01Z",
    ),
    PartBegin(step="run_root/0", part=0, part_type="text"),
    PartDelta(step="run_root/0", part=0, delta=TextDelta("hello")),
    PartEnd(step="run_root/0", part=0, data=TextPart("hello")),
    StepEnd(
        step="run_root/0",
        kind="model",
        status="finished",
        output=(TextPart("hello"),),
        finished_at="2026-01-01T00:00:02Z",
    ),
    RunEnd(
        run="run_root",
        status="finished",
        input=RunControlRef(index=0),
        output=OutputRef(step="run_root/0"),
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
