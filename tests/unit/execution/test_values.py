from __future__ import annotations

from collections.abc import Mapping
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
    ModelParameters,
    ModelRequest,
    ReasoningParameters,
)
from toolang.base.types.policy import RunLimits
from toolang.execution.records import (
    ExecuteControlPayload,
    ControlRecord,
    ReloadControlPayload,
    RetryControlPayload,
    RunControlPayload,
    SteerControlPayload,
    CancelControlPayload,
    control_payload_from_data,
    control_payload_to_data,
    local_from_data,
    local_to_data,
)
from toolang.execution.schemas import ControlInfo
from toolang.execution.types import (
    AgentResources,
    Local,
    Pointer,
    RunCommand,
    StepPath,
    TypedPointer,
    local_from_protocol_data,
    local_to_protocol_data,
)
from toolang.execution.values import parts_from_local
from toolang.lang.input import NamedInputSource, PromptInvocation, RunnableInputRaw
from toolang.lang.types import Array, Struct


def test_pointer_accepts_run_step_control_and_json_paths() -> None:
    assert Pointer("term_1").kind == "thread"
    assert Pointer("run_1").kind == "run"
    assert Pointer("run_1.0.2/key~1name/1").kind == "step"
    assert Pointer("run_1.0.2/key~1name/1").record == "run_1.0.2"
    assert Pointer("run_1.0.2/key~1name/1").field == "/key~1name/1"
    assert Pointer("run_1.0.2/key~1name/1").tokens == ("key/name", "1")
    assert Pointer("run_1@3/payload/locals/1").kind == "control"
    assert Pointer("term_1@0").kind == "control"
    assert str(Pointer.step(StepPath("run_1", (0, 2)), "output", "value")) == (
        "run_1.0.2/output/value"
    )


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
        "run_1/field:name",
    ),
)
def test_pointer_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(ValueError):
        Pointer(value)


def test_pointer_accepts_a_whole_value_slash() -> None:
    assert Pointer("term_1/").field == "/"
    assert Pointer("term_1/").tokens == ("",)


def test_typed_pointer_uses_pointer_then_type() -> None:
    typed = TypedPointer.parse("run_1.0/output/value:Part[]")

    assert typed == TypedPointer("Part[]", Pointer("run_1.0/output/value"))
    assert str(typed) == "run_1.0/output/value:Part[]"
    with pytest.raises(ValueError, match="invalid typed pointer"):
        TypedPointer.parse("Part[]@run_1.0/output/value")
    with pytest.raises(ValueError, match="invalid typed pointer"):
        TypedPointer.parse("run_1/output/value:Part[]:Json")


def test_local_keeps_complete_type_separate_from_execution_dimension() -> None:
    response = Local.typed(
        type_name="Part[]",
        value=(),
        name="_",
        dim=0,
    )
    scattered = Local.typed(
        type_name="Part[]",
        value=Pointer("run_1.0/output/value"),
        name="_",
        dim=1,
    )
    batches = Local.typed(
        type_name="Part[][]",
        value=(
            Pointer("run_a/output/value"),
            Pointer("run_b/output/value"),
        ),
        name="_",
        dim=1,
    )

    assert response.item_type == "Part[]"
    assert scattered.item_type == "Part"
    assert batches.item_type == "Part[]"


def test_dim_one_requires_an_array_type_and_value() -> None:
    with pytest.raises(ValueError, match="array value type"):
        Local(value="one", dim=1)
    with pytest.raises(TypeError, match="Text"):
        Local.typed(type_name="Text[]", value="one", dim=1)


def test_local_codec_round_trips_mixed_concrete_and_pointer_items() -> None:
    local = Local.typed(
        type_name="Part[]",
        value=(
            TextPart("kept"),
            Pointer("run_1.0/output/value/2"),
            ToolCallPart(
                tool_call_id="call_1",
                tool_name="search",
                tool_family="search",
                input={"query": "toolang"},
            ),
        ),
        name="_",
        dim=1,
    )

    assert local_from_data(local_to_data(local)) == local


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
def test_local_codec_round_trips_every_concrete_part(part: Part) -> None:
    local = Local(value=part, name="_")

    data = local_to_data(local)

    stored = cast(Mapping[str, object], data["value"])
    assert stored["?"] == type(part).__name__
    assert local_from_data(data) == local


def test_local_codec_round_trips_structs_and_nested_arrays() -> None:
    local = Local.typed(
        "Review",
        {
            "score": 1,
            "evidence": Array(
                "Part[][]",
                (Array("Part[]", (TextPart("first"),)),),
            ),
        },
        "result",
    )

    assert isinstance(local.value, Struct)
    assert local_from_data(local_to_data(local)) == local


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


def test_local_codec_canonicalizes_untyped_collections_before_storage() -> None:
    local = Local.typed(
        "Review",
        {"items": [1, {"labels": ["one", "two"]}]},
        "result",
    )

    assert isinstance(local.value, Struct)
    assert local.value["items"] == (1, {"labels": ("one", "two")})
    assert local_from_data(local_to_data(local)) == local


def test_local_part_projection_preserves_tool_parts() -> None:
    part = ToolCallPart(
        tool_call_id="call_1",
        tool_name="search",
        tool_family="search",
        input={"query": "toolang"},
    )

    assert parts_from_local(Local.typed("Part[]", (part,), "_", 0)) == (part,)


def test_local_codec_normalizes_collections_and_tags_nested_parts() -> None:
    part = TextPart("nested")
    local = Local.typed(
        type_name="Json",
        value={"items": [part, {"ok": True}]},
        name="_",
    )

    data = local_to_data(local)

    assert local.value == {"items": (part, {"ok": True})}
    assert data["value"] == {
        "?": "Json",
        "items": {
            "?": "Json!",
            "!": [
                {"?": "TextPart", "text": "nested"},
                {"?": "Json", "ok": True},
            ],
        },
    }
    assert local_from_data(data) == local


def test_local_codec_rejects_values_that_do_not_match_the_declared_type() -> None:
    with pytest.raises(ValueError, match="boxed Text"):
        local_from_data(
            {
                "value": {"?": "Text!", "!": 42},
                "name": "_",
                "dim": 0,
            }
        )

    with pytest.raises(TypeError, match="Text"):
        local_from_data(
            {
                "value": {
                    "?": "Json",
                    "items": {"?": "Text[]!", "!": [42]},
                },
                "name": "_",
                "dim": 0,
            }
        )

    with pytest.raises(TypeError, match="Text"):
        local_from_protocol_data({"type": "Text", "value": 42, "name": "_", "dim": 0})


def test_local_codec_reserves_the_pointer_marker() -> None:
    local = Local.typed(type_name="Json", value={"?": "ordinary data"})

    with pytest.raises(ValueError, match="reserved"):
        local_to_data(local)


def test_local_storage_tags_do_not_leak_to_the_protocol_projection() -> None:
    local = Local.typed(
        "Part[]",
        (TextPart("hello"), Pointer("run_1.0/output/value/2")),
        "_",
        1,
    )

    assert local_to_data(local) == {
        "value": {
            "?": "Part[]!",
            "!": [
                {"?": "TextPart", "text": "hello"},
                {"?": "run_1.0/output/value/2:Part"},
            ],
        },
        "name": "_",
        "dim": 1,
    }
    assert local_to_protocol_data(local) == {
        "type": "Part[]",
        "value": [
            {"type": "text", "text": "hello"},
            {"?": "run_1.0/output/value/2:Part"},
        ],
        "name": "_",
        "dim": 1,
    }


def test_protocol_projection_round_trips_parts_nested_in_json() -> None:
    local = Local.typed("Json", {"answer": TextPart("hello")}, "_")

    assert local_to_protocol_data(local)["value"] == {
        "answer": {"type": "text", "text": "hello"}
    }
    assert local_from_protocol_data(local_to_protocol_data(local)) == local


def test_json_preserves_nested_struct_through_durable_projection() -> None:
    local = Local.typed(
        "Json",
        {"review": Struct("Review", {"score": 1})},
        "_",
    )

    assert isinstance(cast(Mapping[str, object], local.value)["review"], Struct)
    assert local_from_data(local_to_data(local)) == local


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
    local = Local.typed("Json", value, "_")

    assert local_from_protocol_data(local_to_protocol_data(local)) == local


def test_preparation_payload_round_trips_resolved_locals() -> None:
    payload = RunControlPayload(
        resources=AgentResources(models=("test/model",)),
        limits=RunLimits(tokens=10),
        state="0" * 64,
        runnable="agic:worker",
        model="test/model",
        model_request=ModelRequest("test/model"),
        locals=(Local.typed("Part[]", (TextPart("hello"),), "_", 0),),
        sandbox="docker:python:3.13-slim",
    )

    data = control_payload_to_data(payload)
    assert data["sandbox"] == "docker:python:3.13-slim"
    assert control_payload_from_data("run", data) == payload


@pytest.mark.parametrize(
    ("reasoning", "expected"),
    [
        (ReasoningParameters(effort="high"), {"effort": "high"}),
        (ReasoningParameters(budget_tokens=4096), {"budget_tokens": 4096}),
    ],
)
def test_preparation_payload_omits_inactive_reasoning_controls(
    reasoning: ReasoningParameters,
    expected: dict[str, object],
) -> None:
    payload = RunControlPayload(
        resources=AgentResources(models=("test/model",)),
        limits=RunLimits(),
        state="0" * 64,
        runnable="agic:worker",
        model="test/model",
        model_request=ModelRequest(
            "test/model",
            ModelParameters(reasoning),
        ),
        locals=(),
    )

    data = control_payload_to_data(payload)

    model_request = cast(dict[str, object], data["model_request"])
    parameters = cast(dict[str, object], model_request["parameters"])
    assert parameters["reasoning"] == expected
    assert control_payload_from_data("run", data) == payload


def test_preparation_payload_preserves_an_absent_model_request() -> None:
    payload = RunControlPayload(
        resources=AgentResources(),
        limits=RunLimits(),
        state="0" * 64,
        runnable="flow:worker",
        model="none",
        model_request=None,
        locals=(),
    )

    data = control_payload_to_data(payload)
    restored = control_payload_from_data("run", data)

    assert data["model_request"] is None
    assert restored == payload


def test_preparation_payload_materializes_a_legacy_model_field() -> None:
    payload = RunControlPayload(
        resources=AgentResources(models=("test/model",)),
        limits=RunLimits(),
        state="0" * 64,
        runnable="agic:worker",
        model="test/model",
        model_request=ModelRequest("test/model"),
        locals=(),
    )
    data = control_payload_to_data(payload)
    data.pop("model_request")

    restored = control_payload_from_data("run", data)

    assert isinstance(restored, RunControlPayload)
    assert restored.model_request == ModelRequest("test/model")


def test_preparation_payload_preserves_a_legacy_model_free_run() -> None:
    payload = RunControlPayload(
        resources=AgentResources(),
        limits=RunLimits(),
        state="0" * 64,
        runnable="flow:worker",
        model="none",
        model_request=None,
        locals=(),
    )
    data = control_payload_to_data(payload)
    data.pop("model_request")

    restored = control_payload_from_data("run", data)

    assert isinstance(restored, RunControlPayload)
    assert restored.model_request is None


def test_preparation_payload_rejects_a_legacy_non_exact_model_ref() -> None:
    payload = RunControlPayload(
        resources=AgentResources(models=("test/*",)),
        limits=RunLimits(),
        state="0" * 64,
        runnable="agic:worker",
        model="test/*",
        model_request=None,
        locals=(),
    )
    data = control_payload_to_data(payload)
    data.pop("model_request")

    with pytest.raises(ValueError, match="model request ref must be exact"):
        control_payload_from_data("run", data)


def test_preparation_payload_rejects_a_mismatched_model_request() -> None:
    with pytest.raises(ValueError, match="model request must match model"):
        RunControlPayload(
            resources=AgentResources(models=("test/model",)),
            limits=RunLimits(),
            state="0" * 64,
            runnable="agic:worker",
            model="test/model",
            model_request=ModelRequest("other/model"),
            locals=(),
        )


def test_preparation_payload_round_trips_authored_prompt_facts() -> None:
    payload = RunControlPayload(
        resources=AgentResources(models=("test/model",)),
        limits=RunLimits(),
        state="0" * 64,
        runnable="agic:worker",
        model="test/model",
        model_request=ModelRequest("test/model"),
        locals=(Local.typed("Part[]", (TextPart("expanded"),), "_", 0),),
        authored_input=RunnableInputRaw(
            _="$review focus=security -- inspect",
            named=(NamedInputSource("tone", "$brief"),),
        ),
        authored_commands=(RunCommand("limit", "time", 30),),
        authored_session_commands=(RunCommand("default", "model", "test/model"),),
        prompt_invocations=(
            PromptInvocation(
                name="review",
                arguments=(("focus", "security"),),
                input_scope="inline",
                parent=None,
                cap_ref="prompt:review",
                content_hash="1" * 64,
            ),
        ),
    )

    data = control_payload_to_data(payload)

    assert data["authored_input"] == {
        "primary": "$review focus=security -- inspect",
        "named": [{"name": "tone", "source": "$brief"}],
    }
    assert control_payload_from_data("run", data) == payload


def test_preparation_payload_reads_legacy_missing_sandbox_as_unknown() -> None:
    payload = RunControlPayload(
        resources=AgentResources(),
        limits=RunLimits(),
        state="0" * 64,
        runnable="flow:worker",
        model="none",
        locals=(),
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
            model="none",
            locals=(),
            sandbox=sandbox,
        )


def test_preparation_payload_rejects_instead_of_dropping_invalid_locals() -> None:
    payload = RunControlPayload(
        resources=AgentResources(models=("test/model",)),
        limits=RunLimits(),
        state="0" * 64,
        runnable="agic:worker",
        model="test/model",
        locals=(Local.typed("Text", "hello", "_", 0),),
    )
    data = control_payload_to_data(payload)
    raw_locals = data["locals"]
    assert isinstance(raw_locals, list)
    cast(list[object], raw_locals).append("invalid")

    with pytest.raises(ValueError, match="invalid local"):
        control_payload_from_data("run", data)


def test_retry_payload_distinguishes_inherited_and_empty_locals() -> None:
    inherited = RetryControlPayload(
        resources=AgentResources(models=("test/model",)),
        limits=RunLimits(),
        state="0" * 64,
        runnable="flow:worker",
        model="test/model",
        model_request=ModelRequest("test/model"),
        locals=None,
        retry_from=StepPath("run_1", (2,)),
    )
    empty = RetryControlPayload(
        resources=inherited.resources,
        limits=inherited.limits,
        state=inherited.state,
        runnable=inherited.runnable,
        model=inherited.model,
        model_request=inherited.model_request,
        locals=(),
        retry_from=None,
    )

    assert control_payload_to_data(inherited)["retry_from"] == "run_1.2"
    assert (
        control_payload_from_data("retry", control_payload_to_data(inherited))
        == inherited
    )
    assert control_payload_from_data("retry", control_payload_to_data(empty)) == empty


def test_reload_and_inherited_preparation_payloads_round_trip_without_revision_duplication() -> (
    None
):
    reload_payload = ReloadControlPayload(state="a" * 64)
    child_payload = RunControlPayload(
        resources=AgentResources(),
        limits=RunLimits(),
        state=None,
        runnable="agic:child",
        model="test/model",
        model_request=ModelRequest("test/model"),
        locals=(),
    )

    assert (
        control_payload_from_data("reload", control_payload_to_data(reload_payload))
        == reload_payload
    )
    child_data = control_payload_to_data(child_payload)
    assert "state" not in child_data
    assert control_payload_from_data("run", child_data) == child_payload


@pytest.mark.parametrize("revision", ("", "A" * 64, "a" * 63, "g" * 64))
def test_reload_payload_rejects_noncanonical_revisions(revision: str) -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        ReloadControlPayload(state=revision)


def test_execute_payload_round_trips_source_pointing_locals() -> None:
    source = Pointer.step(StepPath.parse("run_1.2"), "output", "value", 1)
    payload = ExecuteControlPayload(
        state="a" * 64,
        runnable="flow:deliver",
        module="_flow_deliver",
        source=source,
        locals=(
            Local.typed("Json", source.select("input", "input", "_"), "_"),
            Local.typed(
                "Json",
                source.select("input", "input", "format"),
                "format",
            ),
        ),
    )

    assert (
        control_payload_from_data("execute", control_payload_to_data(payload))
        == payload
    )


@pytest.mark.parametrize(
    ("kind", "payload"),
    (
        (
            "steer",
            SteerControlPayload(
                (Local.typed("Part[]", (TextPart("continue"),), "_", 0),)
            ),
        ),
        ("cancel", CancelControlPayload()),
    ),
)
def test_control_protocol_uses_kind_to_restore_payload_variant(
    kind: Literal["steer", "cancel"],
    payload: SteerControlPayload | CancelControlPayload,
) -> None:
    record = ControlRecord(
        target="run_test",
        index=1,
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
