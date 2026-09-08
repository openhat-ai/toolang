"""Message-format semantics are independent of runtime template generation."""

from dataclasses import replace

import pytest
from pydantic import TypeAdapter

from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.base.types.run import ModelCall
from toolang.execution.events import StepBegin, run_event_from_data, run_event_to_data
from toolang.execution.executor._messages import _MessageBuffer
from toolang.execution.message_delta import (
    delta_from_data,
    delta_to_data,
    literal_delta,
    render_delta,
)
from toolang.execution.types import (
    FieldRef,
    Local,
    MessageDelta,
    MessageTemplate,
    ModelStepGiven,
    StepRef,
    TypedRef,
)
from toolang.lang.types import Array


def reference(type: str) -> TypedRef:
    return TypedRef(
        FieldRef.from_path(StepRef.parse("run_ab12.0"), "output", "local", "value"),
        type,
    )


def test_steer_template_preserves_multimodal_part_boundaries() -> None:
    parts = (
        TextPart("Look here"),
        ImagePart(image_url="https://example.invalid/image"),
    )
    ref = reference("Part[]")
    delta = MessageDelta(
        messages=(MessageTemplate("user", ("<steer>\n", ref, "\n</steer>")),)
    )
    data = delta_to_data(delta)
    assert data == {
        "version": 1,
        "messages": [
            {"role": "user", "segments": ["<steer>\n", {"?": str(ref)}, "\n</steer>"]}
        ],
    }
    assert delta_from_data(data) == delta
    assert render_delta(delta_from_data(data), lambda _: Array("Part[]", parts)) == (
        Message("user", (TextPart("<steer>\n"), *parts, TextPart("\n</steer>"))),
    )


@pytest.mark.parametrize(
    "value",
    [
        Local.typed("Text", "a"),
        Local.typed("TextPart", TextPart("a")),
        Local.typed("Part", ImagePart(file_id="file-image")),
        Local.typed("TextPart[]", (TextPart("a"), TextPart("b"))),
        Local.typed("Part[]", ()),
    ],
)
def test_typed_segments_expand_under_one_rule(value: Local) -> None:
    delta = MessageDelta(messages=(MessageTemplate("user", (reference(value.type),)),))
    expected = (
        (TextPart(value.value),)
        if isinstance(value.value, str)
        else tuple(value.value)
        if isinstance(value.value, Array)
        else (value.value,)
    )
    assert render_delta(delta, lambda _: value.value)[0].parts == expected


def test_literal_parts_and_nested_reference_looking_data_round_trip() -> None:
    tool_data = {
        "?": "run_ab12.0/output/local/value:Part[]",
        "items": [1, {"type": "image"}],
    }
    messages = (
        Message(
            "user",
            (
                TextPart(""),
                TextPart('{"?": "not a reference"}'),
                ImagePart(file_id="image"),
                AudioPart(data="YQ==", format="wav"),
                DocumentPart(file_id="document"),
            ),
        ),
        Message(
            "assistant", (ToolCallPart("call", "search", "search", input=tool_data),)
        ),
        Message(
            "tool", (ToolResultPart("call", "search", "search", output=tool_data),)
        ),
        Message("assistant"),
        Message.user("duplicate"),
        Message.user("duplicate"),
    )
    delta = literal_delta(messages)

    def unexpected(ref: TypedRef) -> object:
        raise AssertionError(f"unexpected reference: {ref}")

    assert delta.messages[0].segments[0] == ""
    assert render_delta(delta_from_data(delta_to_data(delta)), unexpected) == messages


def test_adopted_values_do_not_share_mutable_tool_data() -> None:
    part = ToolResultPart("call", "tool", "tool", output={"items": [1]})
    buffer = _MessageBuffer()
    buffer.append_ref(
        "tool",
        reference("ToolResultPart").ref,
        Local.typed("ToolResultPart", part),
    )
    part.output["items"].append(2)
    adopted = buffer.messages[0].parts[0]
    assert isinstance(adopted, ToolResultPart)
    assert adopted.output == {"items": [1]}


def test_buffer_only_renders_additions_and_groups_unsaved_tool_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import toolang.execution.executor._messages as module

    buffer = _MessageBuffer((Message.user("start"),))
    assert len(buffer.take_delta().messages) == 1
    rendered = []
    original = module.render_delta

    def render(delta, resolve):
        rendered.append(delta)
        return original(delta, resolve)

    monkeypatch.setattr(module, "render_delta", render)
    values = {}
    for index in (1, 2):
        part = ToolResultPart(str(index), "tool", "tool")
        ref = FieldRef.from_path(
            StepRef.from_local("run_ab12", (index,)), "output", "local", "value"
        )
        values[TypedRef(ref, "ToolResultPart")] = part
        buffer.append_ref("tool", ref, Local.typed("ToolResultPart", part))
    buffer.group_tools(0)
    copied = buffer.copy()
    copied.initialize((Message.user("ignored after start"),))
    delta = copied.take_delta()
    assert len(delta.messages) == 1
    assert len(delta.messages[0].segments) == 2
    assert len(rendered) == 2
    assert copied.messages == [
        Message.user("start"),
        *render_delta(delta, values.__getitem__),
    ]
    assert copied.take_delta() == MessageDelta()


def test_unknown_versions_and_non_part_references_fail_explicitly() -> None:
    with pytest.raises(ValueError, match="unsupported message delta version: 2"):
        render_delta(MessageDelta(version=2), lambda _: None)
    with pytest.raises(ValueError, match="unsupported message delta version: 2"):
        delta_from_data({"version": 2, "messages": [{"future": "format"}]})
    with pytest.raises(ValueError, match="requires Text or Parts"):
        render_delta(
            MessageDelta(messages=(MessageTemplate("user", (reference("Json"),)),)),
            lambda _: {},
        )


def test_delta_metadata_does_not_change_public_model_events() -> None:
    call = ModelCall("instruct", [Message.user("hello")])
    given = ModelStepGiven("test/model", call, literal_delta(call.messages))
    event = StepBegin(StepRef.parse("run_ab12.0"), "model", given)
    payload = run_event_to_data(event)
    assert "delta" not in payload["given"]
    assert run_event_from_data(payload) == replace(
        event, given=replace(given, delta=None)
    )
    assert "delta" not in TypeAdapter(ModelStepGiven).dump_python(given, mode="json")
