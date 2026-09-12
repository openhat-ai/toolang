"""Assembly formatting helpers preserve literal content and Part boundaries."""

import pytest

from toolang.base.types.message import ImagePart, TextPart
from toolang.execution.assembly.utils import (
    join_parts,
    strip_parts,
    text_block,
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
