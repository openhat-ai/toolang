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
from toolang.lang.input import coerce_input, coerce_output, perceive_input


def test_plain_input_is_one_text_part_without_rendering_unknown_tags() -> None:
    assert perceive_input("Review {{target}}.\n") == (
        TextPart("Review {{target}}.\n"),
    )


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
            image
            if reference == "diagram.png"
            else pytest.fail("unexpected include")
        ),
    )

    assert result == (
        TextPart("Before\n"),
        image,
        TextPart("\nAfter"),
    )


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
        "Here is the requested value:\n\n"
        "```json\n"
        '["one", "two"]\n'
        "```"
    )

    assert coerce_output(value, "Text[]") == ["one", "two"]


def test_output_coercion_does_not_guess_unfenced_or_ambiguous_json() -> None:
    with pytest.raises(ToolangError, match="output is not valid Text\\[\\]"):
        coerce_output(Message.assistant('Result: ["one", "two"]'), "Text[]")

    with pytest.raises(ToolangError, match="output is not valid Text\\[\\]"):
        coerce_output(
            Message.assistant(
                '```json\n["one"]\n```\n'
                '```json\n["two"]\n```'
            ),
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
