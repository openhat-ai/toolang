"""Canonical execution event codec tests."""

from __future__ import annotations

from toolang.execution.events import ModelStepInfo

from toolang.execution.types import StepRef

from toolang.execution.types import ModelAccounting

from typing import get_args

import pytest
from pydantic import TypeAdapter

from toolang.base.types.message import Message, TextDelta, TextPart
from toolang.base.types.run import ModelCall, ToolCall
from toolang.execution.events import (
    PartBegin,
    PartDelta,
    PartEnd,
    RunEnd,
    StepBegin,
    run_event_from_data,
    run_event_to_data,
)
from toolang.execution.records import StepRecord
from toolang.execution.schemas import RunControlRefData, StepData
from toolang.execution.types import (
    ControlRef,
    ControlStatus,
    FieldRef,
    LoopStepNoted,
    ModelStepGiven,
    ModelStepNoted,
    RunStatus,
    RunRef,
    StepStatus,
    ToolStepGiven,
)
from toolang.lang.ast import RepeatStmt, RunStmt, Span

from toolang.execution.events import (
    EventOutput,
    FlowStepInfo,
    ToolStepInfo,
    ControlApplied,
)
from toolang.execution.observation import step_info, step_summary
from tests.support.observations import step_begin, step_end, run_begin, run_end
from toolang.lang.types import Struct
import json
from toolang.execution.types import TypedRef


def test_step_schema_preserves_the_flow_statement_discriminator() -> None:
    step = StepData(
        path=StepRef.parse("run_root.1"),
        kind="run",
        input=[],
        given=RunStmt(span=Span(line=4), runnable="agic:child"),
        state=RunControlRefData(run="run_root", index=0),
        output=None,
    )

    payload = TypeAdapter(StepData).dump_python(step, mode="json")

    assert payload["path"] == "run_root.1"
    assert payload["state"] == {"run": "run_root", "index": 0}
    assert payload["given"]["kind"] == "run"
    assert TypeAdapter(StepData).validate_python(payload) == step


def test_loop_step_noted_round_trips_through_the_step_schema() -> None:
    step = StepData(
        path=StepRef.parse("run_root.1"),
        kind="loop",
        input=[],
        given=RepeatStmt(span=Span(line=4), count=3),
        state=RunControlRefData(run="run_root", index=0),
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


def test_step_schema_and_record_reject_mismatched_typed_facts() -> None:
    statement = RunStmt(span=Span(line=4), runnable="agic:child")
    with pytest.raises(TypeError, match="tool Step requires ToolStepGiven"):
        StepData(
            path=StepRef.parse("run_root.0"),
            kind="tool",
            input=[],
            given=statement,
            state=RunControlRefData(run="run_root", index=0),
            output=None,
        )
    with pytest.raises(TypeError, match="tool Step requires ToolStepGiven"):
        StepRecord(
            id="run_root.0",
            kind="tool",
            input=(),
            given=statement,
            state=ControlRef.for_run("run_root", 0),
            output=None,
        )

    canonical = StepData(
        path=StepRef.parse("run_root.1"),
        kind="run",
        input=[],
        given=statement,
        state=RunControlRefData(run="run_root", index=0),
        output=None,
    )
    payload = TypeAdapter(StepData).dump_python(canonical, mode="json")
    payload["kind"] = "tool"
    with pytest.raises(TypeError, match="tool Step requires ToolStepGiven"):
        TypeAdapter(StepData).validate_python(payload)


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
    reserved = step_end(
        step=StepRef.parse("run_root.0"),
        kind="value",
        status="pending",
    )
    assert run_event_from_data(run_event_to_data(reserved)) == reserved


@pytest.mark.parametrize(
    "event",
    [
        run_begin("run_root", 0, "agic:test"),
        step_begin("run_root.0", "model", ModelStepInfo("test/model")),
        step_begin("run_root.1", "run", FlowStepInfo("run", runnable="agic:child")),
        step_begin(
            "run_root.2", "tool", ToolStepInfo("shell", "shell__execute", "Run command")
        ),
        PartBegin("run_root.0", 0, "text"),
        PartDelta("run_root.0", 0, TextDelta("hello")),
        PartEnd("run_root.0", 0, TextPart("hello")),
        step_end(
            "run_root.0",
            "model",
            "succeeded",
            EventOutput("Part[]", (TextPart("hello"),)),
        ),
        run_end("run_root", "failed", error="timeout"),
    ],
)
def test_run_event_codec_round_trips_every_event_variant(event):
    assert (
        run_event_from_data(json.loads(json.dumps(run_event_to_data(event)))) == event
    )


@pytest.mark.parametrize("field", ["call", "setup", "state", "messages"])
def test_model_metadata_rejects_recording_details(field):
    data = run_event_to_data(
        step_begin("run_root.0", "model", ModelStepInfo("test/model"))
    )
    data["given"][field] = "unexpected"
    with pytest.raises(ValueError, match="Unexpected keyword"):
        run_event_from_data(data)


def test_model_metadata_size_is_independent_of_call_context():
    def begin(size):
        return step_begin(
            "run_root.0",
            "model",
            ModelStepGiven(
                model="test/model",
                setup="revision",
                call=ModelCall("instructions" * size, [Message.user("history" * size)]),
            ),
        )

    assert run_event_to_data(begin(1)) == run_event_to_data(begin(100000))
    assert run_event_to_data(begin(1))["given"] == {
        "type": "model",
        "model": "test/model",
    }


def test_flow_and_tool_metadata_select_only_presentation_facts():
    flow = step_info(
        RepeatStmt(
            span=Span(line=1),
            count=2,
            stmts=(RunStmt(span=Span(line=2), runnable="child"),),
        )
    )
    tool = step_info(
        ToolStepGiven(
            "shell",
            ToolCall("id", "call", "shell__execute", {"command": "private" * 10000}),
            summary="Execute command",
        )
    )
    assert flow == FlowStepInfo("repeat", binding=None, count=2)
    assert tool == ToolStepInfo("shell", "shell__execute", "Execute command")
    assert "private" not in json.dumps(
        run_event_to_data(step_begin("run_root.0", "tool", tool))
    )


def test_step_events_reject_internal_execution_payloads():
    with pytest.raises(TypeError, match="client Step info"):
        StepBegin("run_root.0", "run_root", None, 0, "model", ModelCall("", []))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="direct data"):
        EventOutput(
            "Text",
            TypedRef(
                FieldRef.from_path(StepRef.parse("run_root.0"), "output", "value"),
                "Text",
            ),
        )
    with pytest.raises(TypeError, match="direct data"):
        EventOutput(
            "Json",
            {"nested": FieldRef.from_path(StepRef.parse("run_root.0"), "output")},
        )


def test_event_output_preserves_literal_reference_shaped_json():
    literal = {
        "?": "user data",
        "hash": "not content",
        "nested": [{"type": "ref", "ref": "literal"}],
    }
    event = step_end("run_root.0", "value", "succeeded", EventOutput("Json", literal))
    assert run_event_from_data(run_event_to_data(event)) == event
    assert run_event_to_data(event)["output"] == {"type": "Json", "value": literal}


def test_struct_output_has_concrete_type_and_direct_data():
    event = step_end(
        "run_root.0",
        "run",
        "succeeded",
        EventOutput("Review", Struct("Review", {"score": 1})),
    )
    assert run_event_from_data(run_event_to_data(event)) == event
    assert event.output is not None and event.output.value == {"score": 1}


def test_errors_and_control_application_have_scalar_identity():
    begin = step_begin(
        "run_root.0",
        "model",
        ModelStepInfo("test/model"),
        preceded_by=(ControlRef.for_run("run_root", 2),),
    )
    end = RunEnd("run_root", "failed", error="model failed", error_source="run_root.0")
    assert begin.preceded_by == (ControlApplied("run_root", 2),)
    assert run_event_from_data(run_event_to_data(end)) == end
    assert run_event_to_data(end)["error"] == "model failed"


def test_model_summary_excludes_continuation_and_pricing_details():
    summary = step_summary(
        ModelStepNoted(
            ModelAccounting(10, 4), continuation={"provider": "private" * 1000}
        )
    )
    assert summary is not None and summary.usage is not None
    assert summary.usage.input_tokens == 10
    data = run_event_to_data(
        step_end("run_root.0", "model", "succeeded", noted=summary)
    )
    assert "continuation" not in json.dumps(data) and "private" not in json.dumps(data)


def test_event_decoder_rejects_old_state_inputs_and_bound_output():
    data = run_event_to_data(
        step_begin("run_root.0", "model", ModelStepInfo("test/model"))
    )
    data["input"] = ["run_root@0/payload/input/_"]
    with pytest.raises(ValueError, match="Unexpected keyword"):
        run_event_from_data(data)
    data = run_event_to_data(
        run_end("run_root", "succeeded", output=EventOutput("Text", "done"))
    )
    data["output"]["binding"] = "_"
    with pytest.raises(ValueError, match="requires type and value"):
        run_event_from_data(data)


@pytest.mark.parametrize("name", ["ReportPart", "ReportPart[]"])
def test_user_struct_names_are_not_inferred_as_canonical_parts(name):
    value = (
        {"type": "ref", "ref": "literal"} if not name.endswith("[]") else [{"score": 2}]
    )
    event = step_end("run_root.0", "value", "succeeded", EventOutput(name, value))
    assert run_event_from_data(run_event_to_data(event)) == event


def test_concrete_part_array_round_trips_without_losing_parts():
    event = step_end(
        "run_root.0",
        "model",
        "succeeded",
        EventOutput("TextPart[]", (TextPart("hello"),)),
    )
    assert run_event_from_data(run_event_to_data(event)) == event


@pytest.mark.parametrize("tool", [False, True])
def test_await_error_is_direct_even_when_target_was_not_observed(tool):
    from toolang.execution.observation import step_end_event
    from toolang.execution.types import ErrorRef
    from toolang.lang.ast import AwaitStmt

    given = (
        ToolStepGiven("runtime", ToolCall("call", "call", "runtime__await", {}))
        if tool
        else AwaitStmt(span=Span(line=1), handle="job")
    )
    record = StepRecord(
        id="run_root.0",
        kind="tool" if tool else "value",
        input=(),
        given=given,
        state=ControlRef.for_run("run_root", 0),
        output=None,
        status="failed",
        error=ErrorRef(FieldRef.from_path(RunRef("run_other"), "error")),
        finished_at="now",
    )
    event = step_end_event(record, None, "target failed")
    assert event.error == "target failed" and event.error_source is None


def test_part_events_reject_nested_runtime_references():
    from toolang.base.types.message import ToolResultPart

    pointer = FieldRef.from_path(StepRef.parse("run_root.0"), "output", "value")
    with pytest.raises(TypeError, match="direct data"):
        PartEnd(
            "run_root.1",
            0,
            ToolResultPart("call", "tool", "tool", output={"nested": pointer}),
        )


@pytest.mark.parametrize("kind", ["Part", "Part[]"])
@pytest.mark.parametrize("value", [None, 1, {}, ["invalid"]])
def test_event_decoder_reports_malformed_part_output_as_a_value_error(kind, value):
    payload = {
        "type": "run_end",
        "run": "run_root",
        "status": "succeeded",
        "output": {"type": kind, "value": value},
    }
    with pytest.raises(ValueError):
        run_event_from_data(payload)


@pytest.mark.parametrize("argument", ["target", "runnable"])
def test_generic_tool_target_named_arguments_do_not_expand_metadata(argument):
    info = step_info(
        ToolStepGiven(
            "shell",
            ToolCall(
                "id",
                "call",
                "shell__execute",
                {argument: "private" * 100000, "async": True},
            ),
            summary="Execute command",
        )
    )
    assert info == ToolStepInfo("shell", "shell__execute", "Execute command")
    event = step_begin("run_root.0", "tool", info)
    assert len(json.dumps(run_event_to_data(event))) < 1024
