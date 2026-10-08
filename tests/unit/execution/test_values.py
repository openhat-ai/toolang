from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Literal, cast

import pytest
from pydantic import TypeAdapter

from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Part,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.base.types.model import (
    ModelRequest,
    Reasoning,
)
from toolang.base.types.policy import RunLimits
from toolang.execution.records import (
    CancelControlPayload,
    ChdirControlPayload,
    ControlRecord,
    ExecControlPayload,
    RetryControlPayload,
    RunControlPayload,
    SteerControlPayload,
    control_payload_from_data,
    control_payload_to_data,
    value_from_data,
    value_to_data,
)
from toolang.execution.schemas import ControlInfo
from toolang.execution.types import (
    value_for_type,
    AgentResources,
    ControlKind,
    ControlRef,
    FieldRef,
    Pointer,
    RunCommand,
    StepRef,
    TypedRef,
    Output,
    output_from_protocol_data,
    output_to_protocol_data,
)
from toolang.execution.values import parts_from_value
from toolang.lang.input import CallInput, PromptInvocation
from toolang.lang.types import Array, Struct, Value


def test_pointer_accepts_run_step_control_and_json_paths() -> None:
    assert Pointer.parse("term_1").kind == "thread"
    assert Pointer.parse("run_1").kind == "run"
    pointer = Pointer.parse("run_1.0.2/key~1name/1")
    field = pointer.field_ref()
    assert pointer.kind == "step"
    assert pointer.record_ref() == StepRef.parse("run_1.0.2")
    assert field is not None and str(field.field) == "/key~1name/1"
    assert pointer.tokens == ("key/name", "1")
    assert Pointer.parse("run_1@3/payload/input/1").kind == "control"
    assert Pointer.parse("term_1@0").kind == "control"
    assert str(
        FieldRef.from_path(StepRef.from_local("run_1", (0, 2)), "output", "value")
    ) == ("run_1.0.2/output/value")


@pytest.mark.parametrize(
    "value",
    (
        "",
        "run_1.01",
        "run_1.0/key~2name",
        "run_1^0",
        "run_1@x/_",
        "run_1@0@1",
        "run@file",
        "term_1.0",
    ),
)
def test_pointer_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(ValueError):
        Pointer.parse(value)


def test_pointer_accepts_a_whole_value_slash() -> None:
    pointer = Pointer.parse("term_1/")
    field = pointer.field_ref()
    assert field is not None and str(field.field) == "/"
    assert pointer.tokens == ("",)


def test_typed_pointer_uses_pointer_then_type() -> None:
    typed = TypedRef.parse("run_1.0/output/value:Part[]")

    assert typed == TypedRef(FieldRef.parse("run_1.0/output/value"), "Part[]")
    assert str(typed) == "run_1.0/output/value:Part[]"
    with pytest.raises(ValueError, match="invalid typed ref"):
        TypedRef.parse("Part[]@run_1.0/output/value")
    with pytest.raises(ValueError, match="invalid typed ref"):
        TypedRef.parse("run_1/output/value:Part[]:Json")


def test_value_keeps_complete_type_without_execution_dimension() -> None:
    response = value_for_type(type_name="Part[]", value=())
    scattered = value_for_type(
        type_name="Part[]",
        value=TypedRef(FieldRef.parse("run_1.0/output/value"), "Part[]"),
    )
    batches = value_for_type(
        type_name="Part[][]",
        value=(
            TypedRef(FieldRef.parse("run_a/output/value"), "Part[]"),
            TypedRef(FieldRef.parse("run_b/output/value"), "Part[]"),
        ),
    )

    assert response.type == "Part[]"
    assert scattered.type == "Part[]"
    assert batches.type == "Part[][]"


def test_array_requires_an_array_value() -> None:
    with pytest.raises(TypeError, match="Text"):
        value_for_type(type_name="Text[]", value="one")


def test_value_codec_round_trips_mixed_concrete_and_pointer_items() -> None:
    local = value_for_type(
        type_name="Part[]",
        value=(
            TextPart("kept"),
            TypedRef(FieldRef.parse("run_1.0/output/value/2"), "Part"),
            ToolCallPart(
                tool_call_id="call_1",
                tool_name="search",
                tool_family="search",
                input={"query": "toolang"},
            ),
        ),
    )

    assert value_from_data(value_to_data(local)) == local


@pytest.mark.parametrize(
    "part",
    (
        TextPart("hello"),
        ImagePart(file_id="image_1", filename="image.png"),
        AudioPart(data="YXVkaW8=", format="mp3", filename="audio.mp3"),
        DocumentPart(url="https://example.test/document.pdf"),
        ToolCallPart(
            tool_call_id="call_1",
            tool_name="search",
            tool_family="search",
            input={"query": "toolang"},
        ),
        ToolResultPart(
            tool_call_id="call_1",
            tool_name="search",
            tool_family="search",
            output={"matches": 1},
        ),
    ),
)
def test_value_codec_round_trips_every_concrete_part(part: Part) -> None:
    local = part

    data = value_to_data(local)

    stored = cast(Mapping[str, object], data)
    assert stored["?"] == type(part).__name__
    assert value_from_data(data) == local


def test_value_codec_round_trips_structs_and_nested_arrays() -> None:
    local = value_for_type(
        "Review",
        {
            "score": 1,
            "evidence": Array("Part[][]", (Array("Part[]", (TextPart("first"),)),)),
        },
    )

    assert isinstance(local, Struct)
    assert value_from_data(value_to_data(local)) == local


@pytest.mark.parametrize(
    "type_name",
    (
        "Text",
        "Number",
        "Boolean",
        "Json",
        "Part",
        "TextPart",
        "ImagePart",
        "AudioPart",
        "DocumentPart",
        "ToolCallPart",
        "ToolResultPart",
    ),
)
def test_struct_rejects_types_reserved_by_runtime_values(type_name: str) -> None:
    with pytest.raises(ValueError, match="built-in type"):
        Struct(type_name, {})


def test_value_codec_canonicalizes_untyped_collections_before_storage() -> None:
    local = value_for_type("Review", {"items": [1, {"labels": ["one", "two"]}]})

    assert isinstance(local, Struct)
    assert local["items"] == (1, {"labels": ("one", "two")})
    assert value_from_data(value_to_data(local)) == local


def test_value_part_projection_preserves_tool_parts() -> None:
    part = ToolCallPart(
        tool_call_id="call_1",
        tool_name="search",
        tool_family="search",
        input={"query": "toolang"},
    )

    assert parts_from_value(value_for_type("Part[]", (part,))) == (part,)


def test_value_codec_normalizes_collections_and_tags_nested_parts() -> None:
    part = TextPart("nested")
    local = value_for_type(type_name="Json", value={"items": [part, {"ok": True}]})

    data = value_to_data(local)

    assert local == {"items": (part, {"ok": True})}
    assert data == {
        "?": "Json",
        "items": {
            "?": "Json!",
            "!": [
                {"?": "TextPart", "text": "nested"},
                {"?": "Json", "ok": True},
            ],
        },
    }
    assert value_from_data(data) == local


def test_value_codec_rejects_values_that_do_not_match_the_declared_type() -> None:
    with pytest.raises(ValueError, match="boxed Text"):
        value_from_data({"?": "Text!", "!": 42})

    with pytest.raises(TypeError, match="Text"):
        value_from_data({"?": "Json", "items": {"?": "Text[]!", "!": [42]}})

    with pytest.raises(TypeError, match="Text"):
        output_from_protocol_data({"type": "Text", "value": 42, "binding": None})


def test_value_codec_reserves_the_pointer_marker() -> None:
    local = value_for_type(type_name="Json", value={"?": "ordinary data"})

    with pytest.raises(ValueError, match="reserved"):
        value_to_data(local)


def test_value_storage_tags_do_not_leak_to_the_protocol_projection() -> None:
    local = value_for_type(
        "Part[]",
        (
            TextPart("hello"),
            TypedRef(FieldRef.parse("run_1.0/output/value/2"), "Part"),
        ),
    )

    assert value_to_data(local) == {
        "?": "Part[]!",
        "!": [
            {"?": "TextPart", "text": "hello"},
            {"?": "run_1.0/output/value/2:Part"},
        ],
    }
    assert output_to_protocol_data(Output(local)) == {
        "binding": None,
        "type": "Part[]",
        "value": [
            {"type": "text", "text": "hello"},
            {"?": "run_1.0/output/value/2:Part"},
        ],
    }


def test_protocol_projection_round_trips_parts_nested_in_json() -> None:
    local = value_for_type("Json", {"answer": TextPart("hello")})

    assert output_to_protocol_data(Output(local))["value"] == {
        "answer": {"type": "text", "text": "hello"}
    }
    assert output_from_protocol_data(output_to_protocol_data(Output(local))) == Output(
        local
    )


def test_json_preserves_nested_struct_through_durable_projection() -> None:
    local = value_for_type("Json", {"review": Struct("Review", {"score": 1})})

    assert isinstance(cast(Mapping[str, object], local)["review"], Struct)
    assert value_from_data(value_to_data(local)) == local


@pytest.mark.parametrize(
    "value",
    (
        {"payload": {"type": "Text[]", "value": ["ordinary"]}},
        {"payload": {"?": "ordinary"}},
        {"payload": {"?": "Text@ordinary"}},
    ),
)
def test_protocol_projection_does_not_reinterpret_ordinary_json(
    value: dict[str, object],
) -> None:
    local = value_for_type("Json", value)

    assert output_from_protocol_data(output_to_protocol_data(Output(local))) == Output(
        local
    )


def test_preparation_payload_round_trips_resolved_input() -> None:
    payload = RunControlPayload(
        resources=AgentResources(models=("test/model",)),
        limits=RunLimits(tokens=10),
        state="0" * 64,
        runnable="agic:worker",
        model_request=ModelRequest("test/model"),
        input=CallInput({"_": Array("Part[]", (TextPart("hello"),))}),
        sandbox="docker:python:3.13-slim",
    )

    data = control_payload_to_data(payload)
    assert data["sandbox"] == "docker:python:3.13-slim"
    assert control_payload_from_data("run", data) == payload


def test_flat_input_codec_retains_presence_types_and_references() -> None:
    from toolang.execution.records import call_input_from_data, call_input_to_data

    pointer = TypedRef(
        FieldRef.from_path(
            ControlRef.for_run("run_source", 0), "payload", "input", "argument"
        ),
        "Text",
    )
    input = CallInput(
        {
            "_": "",
            "zero": 0,
            "disabled": False,
            "nullable": None,
            "items": Array("Text[]", ()),
            "argument": pointer,
            "parts": Array("Part[]", (TextPart("hello"),)),
        }
    )
    encoded = call_input_to_data(input)
    assert encoded["_"] == ""
    assert encoded["zero"] == 0
    assert encoded["disabled"] is False
    assert encoded["items"] == {"?": "Text[]!", "!": []}
    assert call_input_from_data(encoded) == input
    assert call_input_from_data(dict(reversed(tuple(encoded.items())))) == input
    for old in (None, [], [{"value": "old", "dim": 0}]):
        with pytest.raises(ValueError, match="flat object"):
            call_input_from_data(old)


@pytest.mark.parametrize(
    ("reasoning", "expected"),
    [
        (Reasoning(effort="high"), {"effort": "high"}),
        (Reasoning(budget_tokens=4096), {"budget_tokens": 4096}),
    ],
)
def test_preparation_payload_omits_inactive_reasoning_controls(
    reasoning: Reasoning,
    expected: dict[str, object],
) -> None:
    payload = RunControlPayload(
        resources=AgentResources(models=("test/model",)),
        limits=RunLimits(),
        state="0" * 64,
        runnable="agic:worker",
        model_request=ModelRequest("test/model", reasoning=reasoning),
        input=CallInput({}),
    )

    data = control_payload_to_data(payload)

    model_request = cast(dict[str, object], data["model_request"])
    parameters = model_request
    assert parameters["reasoning"] == expected
    assert control_payload_from_data("run", data) == payload


def test_preparation_payload_preserves_an_absent_model_request() -> None:
    payload = RunControlPayload(
        resources=AgentResources(),
        limits=RunLimits(),
        state="0" * 64,
        runnable="flow:worker",
        model_request=None,
        input=CallInput({}),
    )

    data = control_payload_to_data(payload)
    restored = control_payload_from_data("run", data)

    assert data["model_request"] is None
    assert restored == payload


def test_preparation_payload_requires_the_model_request_field() -> None:
    payload = RunControlPayload(
        resources=AgentResources(),
        limits=RunLimits(),
        state=None,
        runnable="agic:worker",
        input=CallInput({}),
        model_request=None,
    )
    data = control_payload_to_data(payload)
    assert "model" not in data
    data.pop("model_request")
    with pytest.raises(ValueError, match="model_request"):
        control_payload_from_data("run", data)


def test_preparation_payload_round_trips_authored_prompt_facts() -> None:
    payload = RunControlPayload(
        resources=AgentResources(models=("test/model",)),
        limits=RunLimits(),
        state="0" * 64,
        runnable="agic:worker",
        model_request=ModelRequest("test/model"),
        input=CallInput({"_": Array("Part[]", (TextPart("expanded"),))}),
        authored_input=CallInput(
            {"_": "$review focus=security -- inspect", "tone": "$brief"}
        ),
        authored_commands=(RunCommand("limit", "time", 30),),
        authored_session_commands=(RunCommand("default", "model", "test/model"),),
        prompt_invocations=(
            PromptInvocation(
                name="review",
                arguments=(("focus", "security"),),
                parent=None,
                cap_ref="prompt:review",
                content_hash="1" * 64,
            ),
        ),
    )

    data = control_payload_to_data(payload)

    assert data["authored_input"] == {
        "_": "$review focus=security -- inspect",
        "tone": "$brief",
    }
    prompt_data = cast(list[dict[str, object]], data["prompt_invocations"])
    assert "input_scope" not in prompt_data[0]
    assert control_payload_from_data("run", data) == payload

    prompt_data[0]["input_scope"] = "inline"
    assert control_payload_from_data("run", data) == payload


def test_preparation_payload_reads_legacy_missing_sandbox_as_unknown() -> None:
    payload = RunControlPayload(
        resources=AgentResources(),
        limits=RunLimits(),
        state="0" * 64,
        runnable="flow:worker",
        model_request=None,
        input=CallInput({}),
    )

    restored = control_payload_from_data("run", control_payload_to_data(payload))

    assert isinstance(restored, RunControlPayload)
    assert restored.sandbox is None
    assert "sandbox" not in control_payload_to_data(restored)


@pytest.mark.parametrize("sandbox", ("", " host", "host "))
def test_preparation_payload_rejects_noncanonical_sandbox(sandbox: str) -> None:
    with pytest.raises(ValueError, match="canonical sandbox"):
        RunControlPayload(
            resources=AgentResources(),
            limits=RunLimits(),
            state="0" * 64,
            runnable="flow:worker",
            model_request=None,
            input=CallInput({}),
            sandbox=sandbox,
        )


def test_preparation_payload_rejects_instead_of_dropping_invalid_input() -> None:
    payload = RunControlPayload(
        resources=AgentResources(models=("test/model",)),
        limits=RunLimits(),
        state="0" * 64,
        runnable="agic:worker",
        model_request=ModelRequest("test/model"),
        input=CallInput({"_": "hello"}),
    )
    data = control_payload_to_data(payload)
    raw_input = data["input"]
    assert isinstance(raw_input, dict)
    cast(dict[str, object], raw_input)["argument"] = {"value": "legacy", "dim": 0}

    with pytest.raises(ValueError, match=r"requires a text \? tag"):
        control_payload_from_data("run", data)


def test_retry_payload_stores_attempt_settings_anchor_and_invalidation() -> None:
    payload = RetryControlPayload(
        resources=AgentResources(models=("test/model",)),
        limits=RunLimits(),
        model_request=ModelRequest("test/model"),
        retry_from=StepRef.from_local("run_1", (2,)),
    )
    data = control_payload_to_data(payload)
    assert set(data) == {
        "resources",
        "limits",
        "model_request",
        "retry_from",
        "invalidated_steps",
        "removed_runs",
    }
    assert data["retry_from"] == "run_1.2"
    assert control_payload_from_data("retry", data) == payload
    unanchored = replace(payload, retry_from=None)
    assert (
        control_payload_from_data("retry", control_payload_to_data(unanchored))
        == unanchored
    )


def test_inherited_preparation_payload_round_trips_without_revision_duplication() -> (
    None
):
    child_payload = RunControlPayload(
        resources=AgentResources(),
        limits=RunLimits(),
        state=None,
        runnable="agic:child",
        model_request=ModelRequest("test/model"),
        input=CallInput({}),
    )

    child_data = control_payload_to_data(child_payload)
    assert "state" not in child_data
    assert control_payload_from_data("run", child_data) == child_payload


def test_exec_payload_round_trips_source_pointing_locals() -> None:
    source = FieldRef.from_path(StepRef.parse("run_1.2"), "output", "value", 1)
    payload = ExecControlPayload(
        state="a" * 64,
        runnable="_flow_deliver$flow:deliver",
        input=CallInput(
            {
                "_": TypedRef(source.select("input", "input", "_"), "Json"),
                "format": TypedRef(source.select("input", "input", "format"), "Json"),
            }
        ),
    )

    assert (
        control_payload_from_data("exec", control_payload_to_data(payload)) == payload
    )


@pytest.mark.parametrize(
    ("kind", "payload"),
    (
        (
            "steer",
            SteerControlPayload(
                CallInput({"_": Array("Part[]", (TextPart("continue"),))})
            ),
        ),
        ("cancel", CancelControlPayload()),
        (
            "exec",
            ExecControlPayload("a" * 64, "flow:next", CallInput({"_": "work"})),
        ),
        ("chdir", ChdirControlPayload("repo://src")),
    ),
)
def test_control_protocol_uses_kind_to_restore_payload_variant(
    kind: Literal["steer", "cancel", "exec", "chdir"],
    payload: SteerControlPayload
    | CancelControlPayload
    | ExecControlPayload
    | ChdirControlPayload,
) -> None:
    record = ControlRecord(
        id="run_test@1",
        kind=kind,
        payload=payload,
    )
    info = ControlInfo(
        run_id="run_test",
        index=record.index,
        kind=kind,
        timing=record.timing,
        request_id=None,
        status=record.status,
        payload=payload,
        error=None,
        created_at="",
        finished_at=None,
    )

    restored_record = TypeAdapter(ControlRecord).validate_python(
        TypeAdapter(ControlRecord).dump_python(record, mode="json")
    )
    restored_info = TypeAdapter(ControlInfo).validate_python(
        TypeAdapter(ControlInfo).dump_python(info, mode="json")
    )

    assert type(restored_record.payload) is type(payload)
    assert type(restored_info.payload) is type(payload)


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("execute", {"state": "a" * 64, "runnable": "flow:next", "input": {}}),
        ("cwd", {"cwd": "repo://src", "cause": "chdir", "state": None}),
    ],
)
def test_renamed_control_kinds_do_not_accept_legacy_aliases(kind, payload) -> None:
    with pytest.raises(ValueError, match="unknown control kind"):
        control_payload_from_data(cast(ControlKind, kind), payload)
    with pytest.raises(ValueError):
        TypeAdapter(ControlRecord).validate_python(
            {"id": "run_test@1", "kind": kind, "payload": payload}
        )


@pytest.mark.parametrize(
    "value",
    [
        [TextPart("nested")],
        {"items": [TextPart("nested")]},
        [[{"answer": TextPart("nested")}]],
    ],
)
def test_parts_projection_accepts_plain_json_arrays(value) -> None:
    assert parts_from_value(value) == parts_from_value(value_for_type("Json", value))


def test_parts_projection_rejects_unresolved_refs_inside_plain_arrays() -> None:
    value = [TypedRef(FieldRef.parse("run_source/output/value"), "Text")]
    with pytest.raises(ValueError, match="resolved"):
        parts_from_value(cast(Value, value))
