from __future__ import annotations

from typing import Any, cast

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Message,
    Part,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.lang.ast import AgicDecl, Field, Parameter, Span, StructDecl
from toolang.lang.errors import ToolangOutputError
from toolang.lang.input import (
    RunnableInput,
    RunnableInputRaw,
    coerce_input,
    coerce_output,
    decode_json_input,
    output_json_schema,
    parse_input,
    resolve_input_parts,
    resolve_input_parts_with_provenance,
    resolve_runnable_input,
)
from toolang.lang.types import Array, Struct


@pytest.mark.parametrize(
    "part",
    (
        TextPart("text"),
        ImagePart(file_id="image-1"),
        AudioPart(data="ZGF0YQ==", format="wav"),
        DocumentPart(file_id="document-1"),
        ToolCallPart(
            tool_call_id="call-1",
            tool_name="lookup",
            tool_family="lookup",
        ),
        ToolResultPart(
            tool_call_id="call-1",
            tool_name="lookup",
            tool_family="lookup",
        ),
    ),
)
def test_part_boundaries_accept_every_concrete_part(part: Part) -> None:
    assert resolve_input_parts((part,)) == (part,)
    assert coerce_input((part,), "Part") is part
    assert coerce_input((part,), "Part[]") == Array("Part[]", (part,))


def test_runnable_input_preserves_primary_and_named_values() -> None:
    image = ImagePart(file_id="image-1")
    runnable = AgicDecl(
        name="review",
        input=Parameter(name="_", type_name="Part[]", span=Span(1)),
        params=(
            Parameter(name="count", type_name="Number", span=Span(1)),
            Parameter(name="metadata", type_name="Json", span=Span(1)),
            Parameter(name="part", type_name="Part", span=Span(1)),
            Parameter(name="parts", type_name="Part[]", span=Span(1)),
        ),
        span=Span(1),
    )
    input = resolve_runnable_input(
        runnable,
        primary=(TextPart("review "), image),
        named={
            "count": 2,
            "metadata": {"enabled": True, "labels": ["one", "two"]},
            "part": image,
            "parts": (TextPart("appendix"), image),
        },
    )

    assert input.primary == Array("Part[]", (TextPart("review "), image))
    assert input.named == {
        "count": 2,
        "metadata": {"enabled": True, "labels": ("one", "two")},
        "part": image,
        "parts": Array("Part[]", (TextPart("appendix"), image)),
    }


def test_runnable_input_rejects_unsupported_runtime_values() -> None:
    with pytest.raises(TypeError, match="unsupported run input value"):
        RunnableInput(named=cast(Any, {"unsupported": {"set"}}))


def test_parse_input_preserves_primary_and_validates_named_sources() -> None:
    assert parse_input(
        "  Review this.\n",
        named=(("focus", "security"), ("count", "2")),
    ) == RunnableInputRaw(
        primary="  Review this.\n",
        named=(("focus", "security"), ("count", "2")),
    )
    assert parse_input(" \t\n") == RunnableInputRaw()


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
    assert resolve_input_parts("Review {{target}}.\n") == (
        TextPart("Review {{target}}.\n"),
    )


def test_typed_part_splice_preserves_multimodal_order() -> None:
    image = ImagePart(image_url="https://example.com/diagram.png")
    document = DocumentPart(file_id="file-1")

    result = resolve_input_parts(
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


def test_structured_parts_are_canonical_data_not_source_syntax() -> None:
    parts = (TextPart("@README.md\n$review target"),)

    assert resolve_input_parts(parts) is parts


def test_include_resolver_inserts_one_typed_part() -> None:
    image = ImagePart(file_id="image-1")

    result = resolve_input_parts(
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
    assert resolve_input_parts(
        " //review\n $review\n @file.md\n::model gpt-5\n//review\n$$review\n@@file.md"
    ) == (
        TextPart(
            " //review\n $review\n @file.md\n:model gpt-5\n/review\n$review\n@file.md"
        ),
    )


def test_markdown_fences_suspend_content_recognition() -> None:
    source = "```text\n$review\n@file.md\n```\nAfter"

    assert resolve_input_parts(source) == (TextPart(source),)


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

    assert resolve_input_parts("$label\nFollowing", program=program) == (
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

    assert resolve_input_parts("$wrap -\nOne\nTwo", program=program) == (
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

    assert resolve_input_parts(
        "$wrap ```\nInside\n```\nOutside",
        program=program,
    ) == (TextPart("[Inside\n]\nOutside"),)

    with pytest.raises(ToolangError, match="Unclosed prompt fence"):
        resolve_input_parts("$wrap ````\nInside\n```", program=program)


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
        resolve_input_parts("$review security", program=program)


def test_inline_prompt_consumes_only_nonempty_current_line_text() -> None:
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

    assert resolve_input_parts(
        "$wrap -- $literal @literal /literal :literal\nOutside",
        program=program,
    ) == (TextPart("[$literal @literal /literal :literal]\nOutside"),)
    with pytest.raises(ToolangError, match="requires nonempty text"):
        resolve_input_parts("$wrap --   ", program=program)


def test_inline_prompt_rejects_multimodal_template_slots() -> None:
    from toolang.lang.ast import CapDecl, Program

    program = Program(
        span=Span(1),
        caps=(
            CapDecl(
                kind="prompt",
                name="outer",
                params=(),
                body="$inner -- {{_}}",
                span=Span(1),
            ),
            CapDecl(
                kind="prompt",
                name="inner",
                params=(),
                body="inner {{_}}",
                span=Span(2),
            ),
        ),
    )

    with pytest.raises(ToolangError, match="requires text-only input"):
        resolve_input_parts(
            "$outer -\n@diagram.png",
            program=program,
            include=lambda _reference: ImagePart(file_id="image-1"),
        )


def test_prompt_resolution_records_ordered_nested_provenance() -> None:
    from toolang.lang.ast import CapDecl, Parameter, Program

    program = Program(
        span=Span(1),
        caps=(
            CapDecl(
                kind="prompt",
                name="outer",
                params=(Parameter(name="focus", span=Span(1)),),
                body="{{focus}} {{_}}",
                span=Span(1),
            ),
            CapDecl(
                kind="prompt",
                name="inner",
                params=(),
                body="inner {{_}}",
                span=Span(2),
            ),
        ),
    )

    resolved = resolve_input_parts_with_provenance(
        "$outer focus=security -\n$inner -- target",
        program=program,
    )

    assert resolved.parts == (TextPart("security inner target"),)
    assert [invocation.name for invocation in resolved.prompts] == ["outer", "inner"]
    assert resolved.prompts[0].arguments == (("focus", "security"),)
    assert resolved.prompts[0].input_scope == "tail"
    assert resolved.prompts[0].parent is None
    assert resolved.prompts[1].input_scope == "inline"
    assert resolved.prompts[1].parent == 0
    assert all(len(invocation.content_hash) == 64 for invocation in resolved.prompts)


def test_slash_prompt_spelling_is_literal_content() -> None:
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

    assert resolve_input_parts("/label", program=program) == (TextPart("/label"),)


def test_input_coercion_preserves_parts_and_parses_declared_values() -> None:
    image = ImagePart(file_id="image-1")

    assert coerce_input((TextPart("hello"),), "Text") == "hello"
    assert coerce_input((TextPart("42"),), "Number") == 42
    assert coerce_input((TextPart("true"),), "Boolean") is True
    assert coerce_input((TextPart('{"ok":true}'),), "Json") == {"ok": True}
    assert coerce_input((image,), "Part") is image
    assert coerce_input((TextPart("look"), image), "Part[]") == Array(
        "Part[]",
        (TextPart("look"), image),
    )

    with pytest.raises(ToolangError, match="non-text parts"):
        coerce_input((TextPart("look"), image), "Text")

    assert coerce_output(TextPart("done"), "Text") == "done"
    assert coerce_output((1, 2), "Number[]") == Array("Number[]", (1, 2))
    assert coerce_input(Array("Number[]", (1, 2)), "Number[]") == Array(
        "Number[]", (1, 2)
    )

    with pytest.raises(ToolangError, match="input is not Number"):
        coerce_input(float("inf"), "Number")


def test_output_coercion_accepts_one_explicit_json_fence() -> None:
    value = Message.assistant(
        'Here is the requested value:\n\n```json\n["one", "two"]\n```'
    )

    assert coerce_output(value, "Text[]") == Array("Text[]", ("one", "two"))


def test_output_coercion_does_not_guess_unfenced_or_ambiguous_json() -> None:
    with pytest.raises(ToolangOutputError, match="output is not valid Text\\[\\]"):
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
    ) == Struct("Review", {"title": "good", "score": 1})

    with pytest.raises(ToolangError, match="unknown Review fields"):
        coerce_input(
            (TextPart('{"title":"good","extra":1}'),),
            "Review",
            structs={"Review": review},
        )


def test_input_coercion_decodes_parts_nested_in_structs() -> None:
    request = StructDecl(
        name="Request",
        fields=(
            Field(name="prompt", type_name="Part", span=Span(1)),
            Field(name="context", type_name="Part[]", span=Span(1)),
        ),
        span=Span(1),
    )

    raw = {
        "prompt": {"type": "text", "text": "Review"},
        "context": [
            "the draft",
            {"type": "image", "file_id": "image-1"},
        ],
    }
    decoded = decode_json_input(
        raw,
        "Request",
        structs={"Request": request},
    )

    assert coerce_input(decoded, "Request", structs={"Request": request}) == Struct(
        "Request",
        {
            "prompt": TextPart("Review"),
            "context": Array(
                "Part[]",
                (TextPart("the draft"), ImagePart(file_id="image-1")),
            ),
        },
    )
    with pytest.raises(ToolangError, match="ordered part sequence"):
        coerce_input("the draft", "Part[]")


def test_output_coercion_keeps_undeclared_assistant_parts() -> None:
    audio = AudioPart(
        data="ZGF0YQ==",
        format="wav",
        transcript="hello",
    )
    message = Message(role="assistant", parts=(audio,))

    assert coerce_output(message, None) == Array("Part[]", (audio,))
    assert coerce_output(Message.assistant("3"), "Number") == 3


def test_output_json_schema_normalizes_scalar_and_unstructured_types() -> None:
    assert output_json_schema(None) is None
    assert output_json_schema("Part") is None
    assert output_json_schema("Part[]") is None
    assert output_json_schema("Text") is None
    assert output_json_schema("Number") == {"type": "number"}
    assert output_json_schema("Boolean") == {"type": "boolean"}
    assert output_json_schema("Json") == {}
    assert output_json_schema("Text[]") == {
        "items": {"type": "string"},
        "type": "array",
    }


def test_output_json_schema_normalizes_optional_and_recursive_structs() -> None:
    node = StructDecl(
        name="Node",
        fields=(
            Field(name="value", type_name="Number", span=Span(1)),
            Field(
                name="children",
                type_name="Node[]",
                optional=True,
                span=Span(1),
            ),
        ),
        span=Span(1),
    )

    assert output_json_schema("Node", structs={"Node": node}) == {
        "$defs": {
            "Node": {
                "additionalProperties": False,
                "properties": {
                    "children": {
                        "items": {"$ref": "#/$defs/Node"},
                        "type": "array",
                    },
                    "value": {"type": "number"},
                },
                "required": ["value"],
                "type": "object",
            }
        },
        "$ref": "#/$defs/Node",
    }


def test_output_json_schema_rejects_unknown_output_types() -> None:
    with pytest.raises(ToolangError, match="unknown Toolang output type: Missing"):
        output_json_schema("Missing")
