"""Canonical execution event codec tests."""

from __future__ import annotations


from toolang.execution.event_projection import event_output

from tests.support.observations import part_end

from toolang.execution.events import ModelStepInfo


from typing import get_args

import pytest
from pydantic import TypeAdapter

from toolang.base.types.message import Message, ReasoningPart, TextDelta, TextPart
from toolang.base.types.run import ModelCall, ToolCall
from toolang.execution.events import (
    PartBegin,
    PartDelta,
    RunEnd,
    StepBegin,
    StepEnd,
    run_event_from_data,
    run_event_to_data,
)
from toolang.execution.records import StepRecord
from toolang.execution.schemas import RunControlRefData, StepData
from toolang.lang.ast import RepeatStmt, RunStmt, Span

from toolang.execution.events import (
    FlowStepInfo,
    ToolStepInfo,
    ControlApplied,
)
from toolang.execution.event_projection import step_info, step_summary
from tests.support.observations import step_begin, step_end, run_begin, run_end
from toolang.lang.types import Struct, Array
import json
from toolang.execution.types import (
    TypedRef,
    TokenUsage,
    ModelStepReport,
    StepRef,
    ControlRef,
    ControlStatus,
    FieldRef,
    LoopStepReport,
    ModelStepSpec,
    RunStatus,
    RunRef,
    StepStatus,
    ToolStepSpec,
)


def test_step_schema_preserves_the_flow_statement_discriminator() -> None:
    step = StepData(
        path=StepRef.parse("run_root.1"),
        kind="run",
        input=[],
        spec=RunStmt(span=Span(line=4), runnable="agic:child"),
        state=RunControlRefData(run="run_root", index=0),
        output=None,
    )

    payload = TypeAdapter(StepData).dump_python(step, mode="json")

    assert payload["path"] == "run_root.1"
    assert payload["state"] == {"run": "run_root", "index": 0}
    assert payload["spec"]["kind"] == "run"
    assert TypeAdapter(StepData).validate_python(payload) == step


def test_loop_step_report_round_trips_through_the_step_schema() -> None:
    step = StepData(
        path=StepRef.parse("run_root.1"),
        kind="loop",
        input=[],
        spec=RepeatStmt(span=Span(line=4), count=3),
        state=RunControlRefData(run="run_root", index=0),
        output=None,
        report=LoopStepReport(iterations=3, termination="exhausted"),
        status="succeeded",
    )

    adapter = TypeAdapter(StepData)
    payload = adapter.dump_python(step, mode="json")

    assert payload["report"] == {
        "iterations": 3,
        "termination": "exhausted",
        "total": None,
    }
    assert adapter.validate_python(payload) == step


def test_step_schema_and_record_reject_mismatched_typed_facts() -> None:
    statement = RunStmt(span=Span(line=4), runnable="agic:child")
    with pytest.raises(TypeError, match="tool Step requires ToolStepSpec"):
        StepData(
            path=StepRef.parse("run_root.0"),
            kind="tool",
            input=[],
            spec=statement,
            state=RunControlRefData(run="run_root", index=0),
            output=None,
        )
    with pytest.raises(TypeError, match="tool Step requires ToolStepSpec"):
        StepRecord(
            id="run_root.0",
            kind="tool",
            input=(),
            spec=statement,
            state=ControlRef.for_run("run_root", 0),
            output=None,
        )

    canonical = StepData(
        path=StepRef.parse("run_root.1"),
        kind="run",
        input=[],
        spec=statement,
        state=RunControlRefData(run="run_root", index=0),
        output=None,
    )
    payload = TypeAdapter(StepData).dump_python(canonical, mode="json")
    payload["kind"] = "tool"
    with pytest.raises(TypeError, match="tool Step requires ToolStepSpec"):
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
        part_end("run_root.0", 0, TextPart("hello")),
        step_end(
            "run_root.0",
            "model",
            "succeeded",
            event_output("Part[]", (TextPart("hello"),)),
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
    data["spec"][field] = "unexpected"
    with pytest.raises(ValueError, match="Unexpected keyword"):
        run_event_from_data(data)


def test_model_metadata_size_is_independent_of_call_context():
    def begin(size):
        return step_begin(
            "run_root.0",
            "model",
            ModelStepSpec(
                model="test/model",
                setup="revision",
                call=ModelCall("instructions" * size, [Message.user("history" * size)]),
            ),
        )

    assert run_event_to_data(begin(1)) == run_event_to_data(begin(100000))
    assert run_event_to_data(begin(1))["spec"] == {
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
        ToolStepSpec(
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
        event_output(
            "Text",
            TypedRef(
                FieldRef.from_path(StepRef.parse("run_root.0"), "output", "value"),
                "Text",
            ),
        )
    with pytest.raises(TypeError, match="direct data"):
        event_output(
            "Json",
            {"nested": FieldRef.from_path(StepRef.parse("run_root.0"), "output")},
        )


def test_event_output_preserves_literal_reference_shaped_json():
    literal = {
        "?": "user data",
        "hash": "not content",
        "nested": [{"type": "ref", "ref": "literal"}],
    }
    event = step_end("run_root.0", "value", "succeeded", event_output("Json", literal))
    assert run_event_from_data(run_event_to_data(event)) == event
    assert run_event_to_data(event)["output"] == {"type": "Json", "value": literal}


def test_struct_output_has_concrete_type_and_direct_data():
    event = step_end(
        "run_root.0",
        "run",
        "succeeded",
        event_output("Review", Struct("Review", {"score": 1})),
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
        ModelStepReport(
            usage=TokenUsage(input_tokens=10, output_tokens=4),
            continuation={"provider": "private" * 1000},
        )
    )
    assert summary is not None and summary.usage is not None
    assert summary.usage.input_tokens == 10
    data = run_event_to_data(
        step_end("run_root.0", "model", "succeeded", report=summary)
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
        run_end("run_root", "succeeded", output=event_output("Text", "done"))
    )
    data["output"]["binding"] = "_"
    with pytest.raises(ValueError, match="Unexpected keyword"):
        run_event_from_data(data)


@pytest.mark.parametrize("name", ["ReportPart", "ReportPart[]"])
def test_user_struct_names_are_not_inferred_as_canonical_parts(name):
    value = (
        {"type": "ref", "ref": "literal"} if not name.endswith("[]") else [{"score": 2}]
    )
    event = step_end("run_root.0", "value", "succeeded", event_output(name, value))
    assert run_event_from_data(run_event_to_data(event)) == event


def test_concrete_part_array_round_trips_without_losing_parts():
    event = step_end(
        "run_root.0",
        "model",
        "succeeded",
        event_output("TextPart[]", (TextPart("hello"),)),
    )
    assert run_event_from_data(run_event_to_data(event)) == event


@pytest.mark.parametrize("kind", ["Part[][]", "TextPart[][]", "Part[][][]"])
def test_nested_part_arrays_retain_final_client_data(kind):
    value = ((TextPart("answer"),),)
    if kind == "Part[][][]":
        value = (value,)
    event = step_end("run_root.0", "value", "succeeded", event_output(kind, value))

    restored = run_event_from_data(json.loads(json.dumps(run_event_to_data(event))))

    assert restored == event
    assert isinstance(restored, StepEnd)
    assert restored.output is not None
    expected = [[{"type": "text", "text": "answer"}]]
    if kind == "Part[][][]":
        expected = [expected]
    assert restored.output.value == expected


def test_nested_part_array_presentation_filters_reasoning_and_metadata():
    from toolang.execution.values import parts_from_value
    from toolang.cli.common.execution_progress.formatting import display_value

    event = step_end(
        "run_root.0",
        "value",
        "succeeded",
        event_output(
            "Part[][]",
            (
                (
                    ReasoningPart("hidden"),
                    TextPart(
                        "answer",
                        signature="private",
                        provider="provider",
                        provider_metadata={"adapter": "test", "model": "test"},
                    ),
                ),
            ),
        ),
    )
    restored = run_event_from_data(run_event_to_data(event))

    assert isinstance(restored, StepEnd)
    assert restored.output is not None
    rendered = parts_from_value(
        display_value(restored.output.value, restored.output.type), content_only=True
    )
    assert rendered == (TextPart('[[{"$part":{"type":"text","text":"answer"}}]]'),)


@pytest.mark.parametrize("tool", [False, True])
def test_await_error_is_direct_even_when_target_was_not_observed(tool):
    from toolang.execution.event_projection import step_end_event
    from toolang.execution.types import ErrorRef
    from toolang.lang.ast import AwaitStmt

    spec = (
        ToolStepSpec("runtime", ToolCall("call", "call", "runtime__await", {}))
        if tool
        else AwaitStmt(span=Span(line=1), handle="job")
    )
    record = StepRecord(
        id="run_root.0",
        kind="tool" if tool else "value",
        input=(),
        spec=spec,
        state=ControlRef.for_run("run_root", 0),
        output=None,
        status="failed",
        error=ErrorRef(FieldRef.from_path(RunRef("run_other"), "error")),
        finished_at="now",
    )
    event = step_end_event(record, None, "target failed")
    assert event.error == "target failed" and event.error_source is None


def test_part_events_reject_internal_values_and_unselected_fields():
    from toolang.execution.events import PartEnd
    from toolang.base.types.message import ToolResultPart

    with pytest.raises(ValueError):
        PartEnd("run_root.1", 0, ToolResultPart("call", "tool", "tool"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PartEnd("run_root.1", 0, {"type": "text", "text": "ok", "signature": "native"})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PartEnd(
            "run_root.1",
            0,
            {  # type: ignore[arg-type]
                "type": "tool_result",
                "tool_call_id": "call",
                "tool_name": "tool",
                "tool_family": "tool",
                "output": {"nested": "body"},
            },
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
        ToolStepSpec(
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


def test_client_part_payload_excludes_native_state_and_tool_bodies():
    from toolang.base.types.message import ToolCallPart, ToolResultPart
    from toolang.execution.event_projection import direct_output
    from toolang.execution.types import Output

    private = "private" * 200000
    parts = (
        TextPart(
            "answer",
            signature=private,
            provider="test",
            provider_metadata={"adapter": "test", "model": "test"},
        ),
        ToolCallPart("call", "shell__execute", "shell", input={"command": private}),
        ToolResultPart("call", "shell__execute", "shell", output={"body": private}),
    )
    for part in parts:
        event = part_end("run_root.0", 0, part)
        assert private not in json.dumps(run_event_to_data(event))
    output = direct_output(Output(Array("Part[]", parts)))
    event = step_end("run_root.0", "model", "succeeded", output)
    assert private not in json.dumps(run_event_to_data(event))


def test_event_encoder_only_serializes_already_projected_data(monkeypatch):
    event = step_end(
        "run_root.0",
        "model",
        "succeeded",
        event_output("Part[]", (TextPart("answer"),)),
    )

    def unexpected_projection(self):
        raise AssertionError("event serialization must not project execution values")

    monkeypatch.setattr(TextPart, "to_data", unexpected_projection)
    assert run_event_to_data(event)["output"]["value"][0]["text"] == "answer"


@pytest.mark.parametrize(
    ("name", "body", "receipt"),
    [
        (
            "runtime__spawn",
            {"id": "run_child", "thread": "thread_child", "status": "pending"},
            {"run": "run_child", "thread": "thread_child"},
        ),
        (
            "runtime__run",
            {"id": "run_child", "thread": "thread_child"},
            {"run": "run_child", "thread": "thread_child"},
        ),
        ("runtime__exec", {"controls": ["run_root@1"]}, {"control_applied": True}),
        ("runtime__exec", {"controls": []}, {"control_applied": False}),
        ("runtime__chdir", {"cwd": "/workspace"}, {"cwd": "/workspace"}),
        (
            "shell__execute",
            {"cwd": "/workspace", "controls": ["literal"], "id": "literal"},
            {},
        ),
    ],
)
def test_tool_client_data_keeps_only_explicit_receipts(name, body, receipt):
    from toolang.base.types.message import ToolResultPart
    from toolang.execution.event_projection import part_data

    body["private"] = "large result" * 100000
    part = ToolResultPart("call", name, name, output=body, call_id="provider-id")
    expected = {
        "type": "tool_result",
        "tool_call_id": "call",
        "tool_name": name,
        "tool_family": name,
        **receipt,
    }
    assert part_data(part) == expected
    event = step_end(
        "run_root.0", "tool", "succeeded", event_output("ToolResultPart", part)
    )
    assert event.output is not None and event.output.value == expected
    assert run_event_from_data(run_event_to_data(event)) == event
    assert len(json.dumps(run_event_to_data(event))) < 1024


def test_media_event_data_preserves_display_content():
    from toolang.base.types.message import ImagePart, AudioPart, DocumentPart
    from toolang.execution.event_projection import part_data

    for part in (
        ImagePart(image_url="data:image/png;base64,abcd", filename="image.png"),
        AudioPart("abcd", "wav", transcript="spoken words"),
        DocumentPart(data="document body", filename="document.txt"),
    ):
        event = part_end("run_root.0", 0, part)
        assert event.data == part.to_data() == part_data(part)
        assert run_event_from_data(run_event_to_data(event)) == event


def test_event_output_constructor_requires_final_protocol_data():
    from toolang.execution.events import EventOutput

    with pytest.raises(ValueError):
        EventOutput("Part[]", [TextPart("internal")])
    event = run_end(
        "run_root",
        "succeeded",
        output=EventOutput("Part[]", [{"type": "text", "text": "answer"}]),
    )
    assert run_event_from_data(run_event_to_data(event)) == event


@pytest.mark.parametrize(
    ("plugin", "name"),
    [
        ("_toolang", "_toolang__run"),
        ("_toolang", "runtime__run"),
        ("runtime", "_toolang__exec"),
        ("runtime", "_toolang__execute"),
        ("runtime", "spawn"),
    ],
)
def test_event_metadata_does_not_interpret_retired_runtime_names(plugin, name):
    spec = ToolStepSpec(
        plugin,
        ToolCall("call", "provider", name, {"runnable": "child", "async": True}),
        summary="Saved action",
    )
    assert step_info(spec) == ToolStepInfo(plugin, name, "Saved action")


@pytest.mark.parametrize(
    "name",
    [
        "_toolang__run",
        "_toolang__spawn",
        "_toolang__exec",
        "_toolang__execute",
        "_toolang__chdir",
        "spawn",
    ],
)
def test_part_data_does_not_interpret_retired_runtime_receipts(name):
    from toolang.base.types.message import ToolResultPart
    from toolang.execution.event_projection import part_data

    part = ToolResultPart(
        "call",
        name,
        "runtime",
        output={
            "id": "run_child",
            "thread": "spawn_child",
            "controls": ["run_root@1"],
            "cwd": "/workspace",
        },
    )
    assert part_data(part) == {
        "type": "tool_result",
        "tool_call_id": "call",
        "tool_name": name,
        "tool_family": "runtime",
    }


def test_retired_await_name_keeps_its_recorded_error_origin():
    from toolang.execution.event_projection import step_end_event
    from toolang.execution.types import ErrorRef

    step = StepRecord(
        id="run_root.0",
        kind="tool",
        input=(),
        spec=ToolStepSpec(
            "_toolang", ToolCall("call", "provider", "_toolang__await", {})
        ),
        state=ControlRef.for_run("run_root", 0),
        output=None,
        status="failed",
        error=ErrorRef(FieldRef.from_path(RunRef("run_other"), "error")),
    )
    assert step_end_event(step, None, "failed").error_source == "run_other"


def test_retired_honor_name_does_not_synthesize_a_runtime_reply():
    from toolang.execution.assembly.tool_replies import workspace_reply_from_step

    step = StepRecord(
        id="run_root.0",
        kind="tool",
        input=(FieldRef.from_path(StepRef.parse("run_root.1"), "output", "value", 0),),
        spec=ToolStepSpec(
            "_toolang",
            ToolCall("call", "provider", "_toolang__honor", {}),
            trigger="runtime",
        ),
        state=ControlRef.for_run("run_root", 0),
        output=None,
    )

    def unexpected_read(value):
        raise AssertionError("retired tool names must not reconstruct a runtime reply")

    assert workspace_reply_from_step(step, unexpected_read) is None


@pytest.mark.parametrize("boundary", ["begin", "end"])
def test_events_reject_retired_recording_field_names(boundary):
    from toolang.execution.events import StepSummary

    if boundary == "begin":
        event = StepBegin(
            "run_contract.0",
            "run_contract",
            None,
            0,
            "tool",
            ToolStepInfo("test", "call"),
        )
        current, retired = "spec", "given"
    else:
        event = StepEnd(
            "run_contract.0",
            "run_contract",
            "tool",
            "succeeded",
            report=StepSummary(summary="done"),
        )
        current, retired = "report", "noted"
    payload = run_event_to_data(event)
    assert run_event_from_data(payload) == event
    payload[retired] = payload.pop(current)
    with pytest.raises(ValueError):
        run_event_from_data(payload)
