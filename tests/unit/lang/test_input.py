from __future__ import annotations

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Message,
    TextPart,
)
from toolang.lang.ast import Field, Span, StructDecl
from toolang.lang.input import (
    RunInput,
    RunInputText,
    coerce_input,
    coerce_output,
    parse_input,
    perceive_input,
)


def test_run_input_round_trips_primary_named_values_and_declared_types() -> None:
    image = ImagePart(file_id="image-1")
    input = RunInput.from_values(
        primary=(TextPart("review "), image),
        named={
            "count": 2,
            "metadata": {"enabled": True, "labels": ["one", "two"]},
            "part": image,
            "parts": (TextPart("appendix"), image),
        },
        types={
            "count": "Number",
            "metadata": "Json",
            "part": "Part",
            "parts": "Part[]",
        },
    )

    restored = RunInput.from_data(input.to_data())

    assert restored == input
    assert restored.values == input.values
    assert restored.types == input.types


def test_run_input_rejects_values_without_a_durable_variant() -> None:
    with pytest.raises(TypeError, match="unsupported run input value"):
        RunInput.from_values(named={"unsupported": {"set"}})


def test_parse_input_preserves_primary_and_validates_named_sources() -> None:
    assert parse_input(
        "  Review this.\n",
        named=(("focus", "security"), ("count", "2")),
    ) == RunInputText(
        primary="  Review this.\n",
        named=(("focus", "security"), ("count", "2")),
    )
    assert parse_input(" \t\n") == RunInputText()


@pytest.mark.parametrize(
    ("source", "named", "message"),
    [
        (":model literal", (), "escape a leading colon"),
        (None, (("1focus", "value"),), "canonical name"),
        (None, (("focus", "one"), ("focus", "two")), "duplicate"),
    ],
)
def test_parse_input_rejects_invalid_sources(
    source: str | None,
    named: tuple[tuple[str, str], ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_input(source, named=named)


def test_plain_input_is_one_text_part_without_rendering_unknown_tags() -> None:
    assert perceive_input("Review {{target}}.\n") == (TextPart("Review {{target}}.\n"),)


def test_typed_part_splice_preserves_multimodal_order() -> None:
    image = ImagePart(image_url="https://example.com/diagram.png")
    document = DocumentPart(file_id="file-1")

    result = perceive_input(
        "Review {{_}} then {{appendix}}.",
        values={
            "_": (TextPart("this "), image),
            "appendix": document,
        },
        types={"_": "Part[]", "appendix": "Part"},
    )

    assert result == (
        TextPart("Review this "),
        image,
        TextPart(" then "),
        document,
        TextPart("."),
    )


def test_structured_percept_is_canonical_data_not_source_syntax() -> None:
    percept = (TextPart("@README.md\n/review target"),)

    assert perceive_input(percept) is percept


def test_include_resolver_inserts_one_typed_part() -> None:
    image = ImagePart(file_id="image-1")

    result = perceive_input(
        "Before\n@diagram.png\nAfter",
        include=lambda reference: (
            image if reference == "diagram.png" else pytest.fail("unexpected include")
        ),
    )

    assert result == (
        TextPart("Before\n"),
        image,
        TextPart("\nAfter"),
    )


def test_content_markers_are_special_only_at_the_start_of_a_line() -> None:
    assert perceive_input(
        " //review\n @file.md\n::model gpt-5\n//review\n@@file.md"
    ) == (TextPart(" //review\n @file.md\n:model gpt-5\n/review\n@file.md"),)


def test_markdown_fences_suspend_content_recognition() -> None:
    source = "```text\n/review\n@file.md\n```\nAfter"

    assert perceive_input(source) == (TextPart(source),)


def test_prompt_without_input_leaves_following_content_outside() -> None:
    from toolang.lang.ast import CapDecl, Program

    program = Program(
        span=Span(1),
        caps=(
            CapDecl(
                kind="prompt",
                name="label",
                params=(),
                body="LABEL",
                span=Span(1),
            ),
        ),
    )

    assert perceive_input("/label\nFollowing", program=program) == (
        TextPart("LABEL\nFollowing"),
    )


def test_tail_prompt_consumes_all_remaining_content() -> None:
    from toolang.lang.ast import CapDecl, Program

    program = Program(
        span=Span(1),
        caps=(
            CapDecl(
                kind="prompt",
                name="wrap",
                params=(),
                body="Before {{_}} after",
                span=Span(1),
            ),
        ),
    )

    assert perceive_input("/wrap -\nOne\nTwo", program=program) == (
        TextPart("Before One\nTwo after"),
    )


def test_fenced_prompt_consumes_only_its_exact_backtick_scope() -> None:
    from toolang.lang.ast import CapDecl, Program

    program = Program(
        span=Span(1),
        caps=(
            CapDecl(
                kind="prompt",
                name="wrap",
                params=(),
                body="[{{_}}]",
                span=Span(1),
            ),
        ),
    )

    assert perceive_input(
        "/wrap ```\nInside\n```\nOutside",
        program=program,
    ) == (TextPart("[Inside\n]\nOutside"),)

    with pytest.raises(ToolangError, match="Unclosed prompt fence"):
        perceive_input("/wrap ````\nInside\n```", program=program)


def test_prompt_arguments_require_named_syntax() -> None:
    from toolang.lang.ast import CapDecl, Parameter, Program

    program = Program(
        span=Span(1),
        caps=(
            CapDecl(
                kind="prompt",
                name="review",
                params=(Parameter(name="focus", span=Span(1)),),
                body="{{focus}}",
                span=Span(1),
            ),
        ),
    )

    with pytest.raises(ToolangError, match="name=value syntax"):
        perceive_input("/review security", program=program)


def test_input_coercion_preserves_parts_and_parses_declared_values() -> None:
    image = ImagePart(file_id="image-1")

    assert coerce_input((TextPart("hello"),), "Text") == "hello"
    assert coerce_input((TextPart("42"),), "Number") == 42
    assert coerce_input((TextPart("true"),), "Boolean") is True
    assert coerce_input((TextPart('{"ok":true}'),), "Json") == {"ok": True}
    assert coerce_input((image,), "Part") is image
    assert coerce_input((TextPart("look"), image), "Part[]") == (
        TextPart("look"),
        image,
    )

    with pytest.raises(ToolangError, match="non-text parts"):
        coerce_input((TextPart("look"), image), "Text")

    assert coerce_output(TextPart("done"), "Text") == "done"
    assert coerce_output((1, 2), "Number[]") == (1, 2)


def test_output_coercion_accepts_one_explicit_json_fence() -> None:
    value = Message.assistant(
        'Here is the requested value:\n\n```json\n["one", "two"]\n```'
    )

    assert coerce_output(value, "Text[]") == ["one", "two"]


def test_output_coercion_does_not_guess_unfenced_or_ambiguous_json() -> None:
    with pytest.raises(ToolangError, match="output is not valid Text\\[\\]"):
        coerce_output(Message.assistant('Result: ["one", "two"]'), "Text[]")

    with pytest.raises(ToolangError, match="output is not valid Text\\[\\]"):
        coerce_output(
            Message.assistant('```json\n["one"]\n```\n```json\n["two"]\n```'),
            "Text[]",
        )


def test_struct_coercion_validates_fields() -> None:
    review = StructDecl(
        name="Review",
        fields=(
            Field(name="title", type_name="Text", span=Span(1)),
            Field(
                name="score",
                type_name="Number",
                optional=True,
                span=Span(1),
            ),
        ),
        span=Span(1),
    )

    assert coerce_input(
        (TextPart('{"title":"good","score":1}'),),
        "Review",
        structs={"Review": review},
    ) == {"title": "good", "score": 1}

    with pytest.raises(ToolangError, match="unknown Review fields"):
        coerce_input(
            (TextPart('{"title":"good","extra":1}'),),
            "Review",
            structs={"Review": review},
        )


def test_output_coercion_keeps_undeclared_assistant_percept() -> None:
    audio = AudioPart(
        data="ZGF0YQ==",
        format="wav",
        transcript="hello",
    )
    message = Message(role="assistant", parts=(audio,))

    assert coerce_output(message, None) == (audio,)
    assert coerce_output(Message.assistant("3"), "Number") == 3
