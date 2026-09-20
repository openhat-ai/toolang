"""Message-format semantics are independent of runtime template generation."""

from dataclasses import replace
from typing import Any, cast

import pytest
from pydantic import TypeAdapter

from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Message,
    MessageRecall,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.base.types.run import ModelCall
from toolang.execution.events import StepBegin, run_event_from_data, run_event_to_data
from toolang.execution.assembly.message_buffer import MessageBuffer
from toolang.execution.assembly.utils import literal_delta, render_delta
from toolang.execution.records import (
    ModelCallRefs,
    StoredModelStepGiven,
    delta_from_data,
    delta_to_data,
    stored_step_given_from_data,
    stored_step_given_to_data,
)
from toolang.execution.recall import recall_revisions
from toolang.execution.types import (
    FieldRef,
    Local,
    ModelMessages,
    ContentRef,
    MessageTemplate,
    ModelStepGiven,
    StepRef,
    TypedRef,
    SkillRecallTarget,
    SkillTriggerRecallTarget,
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
    delta = (MessageTemplate("user", ("<steer>\n", ref, "\n</steer>")),)
    data = delta_to_data(delta)
    assert data == [
        {
            "role": "user",
            "content": {"segments": ["<steer>\n", {"?": str(ref)}, "\n</steer>"]},
        }
    ]
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
    delta = (MessageTemplate("user", (reference(value.type),)),)
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

    def unexpected(ref: TypedRef | ContentRef) -> object:
        raise AssertionError(f"unexpected reference: {ref}")

    assert delta[0].content[0] == ""
    assert render_delta(delta_from_data(delta_to_data(delta)), unexpected) == messages


def test_adopted_values_do_not_share_mutable_tool_data() -> None:
    part = ToolResultPart("call", "tool", "tool", output={"items": [1]})
    buffer = MessageBuffer()
    buffer.append_ref(
        "tool",
        reference("ToolResultPart").ref,
        Local.typed("ToolResultPart", part),
    )
    part.output["items"].append(2)
    adopted = buffer.messages[0].parts[0]
    assert isinstance(adopted, ToolResultPart)
    assert adopted.output == {"items": [1]}


def test_resolved_messages_are_copied_without_reference_rendering(monkeypatch) -> None:
    import toolang.execution.assembly.message_buffer as module

    def unexpected(*args, **kwargs):
        pytest.fail("resolved messages must not pass through reference rendering")

    monkeypatch.setattr(module, "render_delta", unexpected)
    part = ToolResultPart("call", "tool", "tool", output={"items": [1]})
    message = Message("tool", (part,))
    buffer = MessageBuffer((message,))
    part.output["items"].append(2)
    adopted = buffer.messages[0].parts[0]
    recorded = buffer.templates[0].content[0]
    assert isinstance(adopted, ToolResultPart)
    assert isinstance(recorded, ToolResultPart)
    assert adopted.output == recorded.output == {"items": [1]}

    candidate = buffer.copy()
    candidate.append(Message.user("pending"))
    candidate.take_delta(StepRef.parse("run_ab12.0"))
    assert buffer.head is None
    assert len(buffer.messages) == len(buffer.templates) == len(buffer.pending) == 1


def test_buffer_only_renders_additions_and_groups_unsaved_tool_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import toolang.execution.assembly.message_buffer as module

    buffer = MessageBuffer((Message.user("start"),))
    head = StepRef.parse("run_ab12.0")
    assert len(buffer.take_delta(head).delta) == 1
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
    delta = copied.take_delta(StepRef.parse("run_ab12.3"))
    assert delta.head == head
    assert len(delta.delta) == 1
    assert len(delta.delta[0].content) == 2
    assert len(rendered) == 2
    assert copied.messages == [
        Message.user("start"),
        *render_delta(delta.delta, values.__getitem__),
    ]
    assert copied.take_delta(StepRef.parse("run_ab12.4")) == ModelMessages(head)


def test_unknown_versions_and_non_part_references_fail_explicitly() -> None:
    with pytest.raises(ValueError, match="unsupported durable model call version: 2"):
        ModelCallRefs(
            "hash",
            ModelMessages(StepRef.parse("run_ab12.0")),
            None,
            None,
            None,
            version=2,
        )
    with pytest.raises(ValueError, match="must be an array"):
        delta_from_data({"version": 2, "messages": [{"future": "format"}]})
    with pytest.raises(ValueError, match="requires Text or Parts"):
        render_delta(
            (MessageTemplate("user", (reference("Json"),)),),
            lambda _: {},
        )


def test_delta_metadata_does_not_change_public_model_events() -> None:
    call = ModelCall("instruct", [Message.user("hello")])
    given = ModelStepGiven(
        "test/model",
        call,
        ModelMessages(StepRef.parse("run_ab12.0"), literal_delta(call.messages)),
        setup="test-setup",
    )
    event = StepBegin(StepRef.parse("run_ab12.0"), "model", given)
    payload = run_event_to_data(event)
    assert "messages" not in payload["given"]
    assert run_event_from_data(payload) == replace(
        event, given=replace(given, messages=None)
    )
    assert "messages" not in TypeAdapter(ModelStepGiven).dump_python(given, mode="json")


def test_shared_resource_ref_keeps_trigger_and_guidance_visibility_separate() -> None:
    revision = "a" * 64
    messages = tuple(
        Message(
            "user",
            (TextPart("literal {{text}}"),),
            tag=tag,
            recall=MessageRecall("skill/testing", version),
        )
        for tag, version in (
            ("skill-trigger", revision),
            ("skill-guidance", revision),
            ("skill-guidance", "0"),
        )
    )
    delta = literal_delta(messages)
    restored = render_delta(delta_from_data(delta_to_data(delta)), lambda _: None)
    assert [(m.tag, m.recall) for m in restored] == [
        (m.tag, m.recall) for m in messages
    ]
    assert recall_revisions(restored) == {
        SkillTriggerRecallTarget("skill/testing"): revision,
        SkillRecallTarget("skill/testing"): "0",
    }
    assert messages[1].recall == MessageRecall("skill/testing", revision)
    public = messages[1].to_data()
    assert set(public) == {"role", "parts"}
    assert set(TypeAdapter(Message).dump_python(messages[1], mode="json")) == {
        "role",
        "parts",
    }
    forged = Message.from_data(
        {
            **public,
            "tag": "skill-guidance",
            "recall": {"ref": "skill/testing", "revision": revision},
        }
    )
    assert forged.tag is None and forged.recall is None
    assert recall_revisions((forged,)) == {}


@pytest.mark.parametrize("version", [0, 2, True, "1", None])
def test_call_version_is_checked_before_decoding_future_messages(version) -> None:
    given = StoredModelStepGiven(
        "test/model",
        ModelCallRefs(
            "hash", ModelMessages(StepRef.parse("run_ab12.0")), None, None, None
        ),
        setup="test-setup",
    )
    data = stored_step_given_to_data("model", given)
    call = cast(dict[str, Any], data["call"])
    assert set(call["messages"]) == {"head", "delta"}
    assert "recall" not in call and "delta" not in call
    call["version"] = version
    call["messages"] = {"future": "unknown format"}
    with pytest.raises(ValueError, match="unsupported durable model call version"):
        stored_step_given_from_data("model", data)


@pytest.mark.parametrize(
    "message",
    [
        {"role": "system", "content": {"segments": []}},
        {"role": "user", "content": {"segments": "not an array"}},
        {"role": "user", "content": {"segments": [], "escape_text": 1}},
        {"role": "user", "content": {"segments": [{"hash": "invalid"}]}},
        {
            "role": "user",
            "content": {"segments": []},
            "recall": {"ref": "skill/testing", "revision": "0"},
        },
        {
            "role": "user",
            "content": {"segments": []},
            "tag": "skill-guidance",
            "recall": {"ref": "", "revision": "0"},
        },
        {
            "role": "user",
            "content": {"segments": []},
            "tag": "skill-guidance",
            "recall": {"ref": "skill/testing", "revision": 0},
        },
        {"role": "user", "content": {"segments": []}, "tag": " "},
        {
            "role": "user",
            "content": {"segments": []},
            "source": "home://skills/testing",
        },
    ],
)
def test_malformed_message_records_are_rejected(message) -> None:
    with pytest.raises((ValueError, TypeError)):
        delta_from_data([message])
