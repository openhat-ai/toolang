"""Assembly formatting helpers preserve literal content and Part boundaries."""

import pytest

from toolang.base.types.message import ImagePart, Message, ReasoningPart, TextPart
from toolang.execution.assembly.utils import (
    join_parts,
    strip_parts,
    text_block,
    literal_delta,
    render_delta,
)


@pytest.mark.parametrize(
    "content,expected",
    [
        ("", ""),
        ('</context>&"', '<context>\n&lt;/context&gt;&amp;"\n</context>'),
    ],
)
def test_text_block_frames_literal_content(content: str, expected: str) -> None:
    assert text_block("context", content) == expected


def test_strip_parts_trims_only_outer_text_without_mutating_parts() -> None:
    image = ImagePart(file_id="image")
    parts = (TextPart(" \n"), image, TextPart(" Tail \n"))

    assert strip_parts(parts) == (image, TextPart(" Tail"))
    assert parts[0] == TextPart(" \n")
    assert parts[-1] == TextPart(" Tail \n")
    assert strip_parts(()) == ()


def test_join_parts_merges_adjacent_text_and_preserves_nontext_boundaries() -> None:
    image = ImagePart(file_id="image")

    assert join_parts(
        (TextPart("one"), TextPart(" two")), (), (image,), (TextPart("tail"),)
    ) == (TextPart("one two\n\n"), image, TextPart("\n\ntail"))
    assert join_parts((), ()) == ()


def test_native_text_boundaries_and_whitespace_survive_history_assembly():
    origin: dict[str, object] = {"adapter": "generate_content", "model": "model"}
    parts = (
        TextPart(
            "  first\n", signature="one", provider="provider", provider_metadata=origin
        ),
        TextPart("", signature="empty", provider="provider", provider_metadata=origin),
        ReasoningPart("  thought\n", "thought", "provider", origin),
        TextPart(
            "last  ", signature="last", provider="provider", provider_metadata=origin
        ),
    )
    assert strip_parts(parts) == parts
    assert join_parts(parts) == parts
    assert render_delta(
        literal_delta((Message("assistant", parts),)), lambda value: value
    ) == (Message("assistant", parts),)
