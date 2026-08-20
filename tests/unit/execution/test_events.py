"""Canonical execution event codec tests."""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import TypeAdapter

from toolang.base.types.message import TextDelta, TextPart
from toolang.base.types.run import ModelCall, ToolCall
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
from toolang.execution.records import StepRecord
from toolang.execution.schemas import StepData
from toolang.execution.types import (
    CollectionStepNoted,
    ControlRef,
    ControlStatus,
    Local,
    LoopStepNoted,
    ModelStepGiven,
    ModelStepNoted,
    ModelTokenCount,
    Occurrence,
    OccurrencePosition,
    Pointer,
    RunStatus,
    StepPath,
    StepStatus,
    ToolStepGiven,
)
from toolang.lang.ast import KeepStmt, RepeatStmt, RunStmt, ScatterStmt, Span


_EVENTS: tuple[RunEvent, ...] = (
    RunBegin(
        run="run_root",
        control=ControlRef("run_root", 0),
        runnable="agic:test",
        occurrence=Occurrence(item=OccurrencePosition(index=0, count=2)),
        started_at="2026-01-01T00:00:00Z",
    ),
    StepBegin(
        step=StepPath.parse("run_root.0"),
        kind="model",
        given=ModelStepGiven(model="test/model", call=ModelCall("", [])),
        input=(
            Pointer.control("run_root", 0, "_"),
            Pointer.step(StepPath.parse("run_root.1")),
        ),
        started_at="2026-01-01T00:00:01Z",
    ),
    StepBegin(
        step=StepPath.parse("run_root.1"),
        kind="run",
        given=RunStmt(
            span=Span(line=4),
            doc="Run the child.",
            runnable="agic:child",
        ),
    ),
    StepBegin(
        step=StepPath.parse("run_root.2"),
        kind="tool",
        given=ToolStepGiven(
            plugin="shell",
            call=ToolCall("tool-1", "call-1", "shell__execute", {"command": "pwd"}),
        ),
    ),
    PartBegin(step=StepPath.parse("run_root.0"), part=0, part_type="text"),
    PartDelta(step=StepPath.parse("run_root.0"), part=0, delta=TextDelta("hello")),
    PartEnd(step=StepPath.parse("run_root.0"), part=0, data=TextPart("hello")),
    StepEnd(
        step=StepPath.parse("run_root.0"),
        kind="model",
        status="succeeded",
        output=Local.typed("Part[]", (TextPart("hello"),), "_", 0),
        noted=ModelStepNoted(tokens=ModelTokenCount(input=4, output=2)),
        finished_at="2026-01-01T00:00:02Z",
    ),
    RunEnd(
        run="run_root",
        status="succeeded",
        output=Local.typed(
            "Part[]",
            Pointer.step(StepPath.parse("run_root.0")),
            "_",
            0,
        ),
        finished_at="2026-01-01T00:00:03Z",
    ),
)


def test_run_event_codec_round_trips_every_event_variant() -> None:
    for event in _EVENTS:
        assert run_event_from_data(run_event_to_data(event)) == event


def test_step_schema_preserves_the_flow_statement_discriminator() -> None:
    step = StepData(
        path=StepPath.parse("run_root.1"),
        kind="run",
        input=[],
        given=ScatterStmt(span=Span(line=4), count=2, runnable="agic:child"),
        output=None,
    )

    payload = TypeAdapter(StepData).dump_python(step, mode="json")

    assert payload["path"] == "run_root.1"
    assert payload["given"]["kind"] == "scatter"
    assert TypeAdapter(StepData).validate_python(payload) == step


def test_run_event_codec_rejects_unknown_discriminator() -> None:
    with pytest.raises(ValueError, match="unknown"):
        run_event_from_data(
            {
                "run": "run_root",
                "input": {"index": 0, "part": None},
                "type": "unknown",
            }
        )


def test_step_events_reject_mismatched_typed_facts() -> None:
    with pytest.raises(TypeError, match="value Step requires a compatible FlowStmt"):
        StepBegin(
            step=StepPath.parse("run_root.0"),
            kind="value",
            given=ModelStepGiven(model="test/model", call=ModelCall("", [])),
        )
    with pytest.raises(TypeError, match="tool Step does not accept noted facts"):
        StepEnd(
            step=StepPath.parse("run_root.0"),
            kind="tool",
            status="succeeded",
            noted=ModelStepNoted(),
        )
    with pytest.raises(ValueError, match="successful termination"):
        StepEnd(
            step=StepPath.parse("run_root.1"),
            kind="loop",
            status="succeeded",
            noted=LoopStepNoted(iterations=1, termination="failed"),
        )
    with pytest.raises(ValueError, match="requires collection output items"):
        StepEnd(
            step=StepPath.parse("run_root.2"),
            kind="value",
            status="succeeded",
            noted=CollectionStepNoted(total_items=2),
        )
    with pytest.raises(ValueError, match="cannot have collection output items"):
        StepEnd(
            step=StepPath.parse("run_root.3"),
            kind="par",
            status="failed",
            noted=CollectionStepNoted(total_items=2, output_items=1),
        )


def test_loop_step_noted_round_trips_with_its_terminal_cause() -> None:
    begin = StepBegin(
        step=StepPath.parse("run_root.1"),
        kind="loop",
        given=RepeatStmt(span=Span(line=4), count=3),
    )
    end = StepEnd(
        step=begin.step,
        kind="loop",
        status="succeeded",
        noted=LoopStepNoted(iterations=3, termination="exhausted", total=3),
    )

    assert run_event_from_data(run_event_to_data(begin)) == begin
    assert run_event_from_data(run_event_to_data(end)) == end


def test_loop_step_noted_round_trips_through_the_step_schema() -> None:
    step = StepData(
        path=StepPath.parse("run_root.1"),
        kind="loop",
        input=[],
        given=RepeatStmt(span=Span(line=4), count=3),
        output=None,
        noted=LoopStepNoted(iterations=3, termination="exhausted"),
        status="succeeded",
    )

    adapter = TypeAdapter(StepData)
    payload = adapter.dump_python(step, mode="json")

    assert payload["noted"] == {
        "iterations": 3,
        "termination": "exhausted",
        "total": None,
    }
    assert adapter.validate_python(payload) == step


def test_collection_step_noted_round_trips_with_cardinality() -> None:
    begin = StepBegin(
        step=StepPath.parse("run_root.1"),
        kind="value",
        given=KeepStmt(span=Span(line=4), position="first", count=2),
    )
    end = StepEnd(
        step=begin.step,
        kind="value",
        status="succeeded",
        noted=CollectionStepNoted(total_items=6, output_items=2),
    )

    assert run_event_from_data(run_event_to_data(begin)) == begin
    assert run_event_from_data(run_event_to_data(end)) == end


def test_step_schema_and_record_reject_mismatched_typed_facts() -> None:
    statement = RunStmt(span=Span(line=4), runnable="agic:child")
    with pytest.raises(TypeError, match="tool Step requires ToolStepGiven"):
        StepData(
            path=StepPath.parse("run_root.0"),
            kind="tool",
            input=[],
            given=statement,
            output=None,
        )
    with pytest.raises(TypeError, match="tool Step requires ToolStepGiven"):
        StepRecord(
            path=StepPath.parse("run_root.0"),
            kind="tool",
            input=(),
            given=statement,
            output=None,
        )

    canonical = StepData(
        path=StepPath.parse("run_root.1"),
        kind="run",
        input=[],
        given=statement,
        output=None,
    )
    payload = TypeAdapter(StepData).dump_python(canonical, mode="json")
    payload["kind"] = "tool"
    with pytest.raises(TypeError, match="tool Step requires ToolStepGiven"):
        TypeAdapter(StepData).validate_python(payload)


def test_begin_events_reject_loose_occurrence_payloads() -> None:
    with pytest.raises(TypeError, match="Occurrence or None"):
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            occurrence={  # type: ignore[arg-type]
                "item": {"index": 0, "count": 1}
            },
        )


def test_flow_given_codec_rejects_noncanonical_fields() -> None:
    payload = run_event_to_data(
        StepBegin(
            step=StepPath.parse("run_root.0"),
            kind="run",
            given=RunStmt(span=Span(line=4), runnable="agic:child"),
        )
    )
    payload["given"]["extra"] = True

    with pytest.raises(ValueError, match="canonical typed fields"):
        run_event_from_data(payload)


def test_run_event_codec_rejects_legacy_placement() -> None:
    with pytest.raises(ValueError, match="occurrence instead of placement"):
        run_event_from_data(
            {
                "type": "run_begin",
                "run": "run_root",
                "control": "run_root@0",
                "placement": {"item": 0, "items": 1},
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
        step=StepPath.parse("run_root.0"),
        kind="value",
        status="pending",
    )
    assert run_event_from_data(run_event_to_data(reserved)) == reserved


def test_run_event_codec_serializes_step_error_references() -> None:
    event = RunEnd(
        run="run_root",
        status="failed",
        error=Pointer.step(StepPath.parse("run_root.2")),
    )

    data = run_event_to_data(event)

    assert data["error"] == {"?": "@run_root.2"}
    assert run_event_from_data(data) == event


def test_run_event_codec_distinguishes_run_error_pointers_from_messages() -> None:
    pointer = RunEnd(
        run="run_root",
        status="failed",
        error=Pointer.run("run_child"),
    )
    message = RunEnd(run="run_root", status="failed", error="timeout")

    assert run_event_to_data(pointer)["error"] == {"?": "@run_child"}
    assert run_event_from_data(run_event_to_data(pointer)) == pointer
    assert run_event_from_data(run_event_to_data(message)) == message


def test_run_event_codec_round_trips_struct_output() -> None:
    event = StepEnd(
        step=StepPath.parse("run_root.0"),
        kind="run",
        status="succeeded",
        output=Local.typed("Review", {"score": 1}, "_"),
    )

    assert run_event_from_data(run_event_to_data(event)) == event
