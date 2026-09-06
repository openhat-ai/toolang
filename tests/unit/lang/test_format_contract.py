"""Formatting preserves the semantic content of every grammar-owned text body."""

import pytest

from toolang.lang import Program, format_source, to_data
from toolang.lang.errors import ToolangFormatError, ToolangSyntaxError


def _semantics(source: str) -> object:
    program = Program.from_source(source)
    names = {
        item.name: f"<{kind}:{index}>"
        for kind in ("agics", "contexts", "instructs")
        for index, item in enumerate(getattr(program, kind))
        if item.name.startswith("<")
    }

    def normalize(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: normalize(item) for key, item in value.items() if key != "span"
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            return names.get(value, value)
        return value

    return normalize(to_data(program))


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize("gap", ["", "\n"])
def test_implicit_text_indentation_does_not_create_paragraphs(
    kind: str, gap: str
) -> None:
    source = f"{kind} work:\n  First.\n{gap}    Nested.\n  Last.\n"
    formatted = format_source(source)
    assert _semantics(formatted) == _semantics(source)
    assert format_source(formatted) == formatted


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_unicode_text_separators_do_not_create_source_lines(separator: str) -> None:
    source = f"flow work:\n  run:\n    First.{separator}Second.\n    Last.\n"
    formatted = format_source(source)
    assert _semantics(formatted) == _semantics(source)
    assert format_source(formatted) == formatted


def test_unicode_text_does_not_shift_documentation_or_diagnostics() -> None:
    prefix = "context notes:\n  First.\u2028Second.\n\n"
    valid = prefix + "## Work documentation.\nflow work:\n  Work.\n"
    assert Program.from_source(valid).flows[0].doc == "Work documentation."
    invalid = prefix + "flow work:\n  sort these items\n"
    with pytest.raises(ToolangSyntaxError, match="line 5.*sort these items"):
        Program.from_source(invalid)
    with pytest.raises(ToolangFormatError, match="line 5.*sort these items"):
        format_source(invalid)


def test_control_headers_use_cst_body_shape() -> None:
    source = (
        "agic work:\n"
        "  context: # Literal context follows.\n"
        "    First.\n\n    Second.\n"
        "  instruct: Keep this:\n"
        "  Work.\n"
    )
    formatted = format_source(source)
    assert formatted == (
        "agic work:\n"
        "  instruct: Keep this:\n\n"
        "  context: # Literal context follows.\n"
        "    First.\n\n    Second.\n\n"
        "  Work.\n"
    )
    assert _semantics(formatted) == _semantics(source)
    assert format_source(formatted) == formatted


@pytest.mark.parametrize("tab_size", [1, 2, 4])
@pytest.mark.parametrize(
    "header, depth",
    [
        ("flow work:\n  run:", 4),
        ("flow work:\n  map using:", 4),
        ("flow work:\n  repeat 2 times:\n    run: Work.\n    until:", 6),
        ("flow work:\n  let note =", 4),
        ("flow work:\n  ask:", 4),
        ("agic work:\n  user:", 4),
        ("agic work:\n  context:", 4),
        ("agic work:\n  instruct:", 4),
        ("context notes:", 2),
        ("instruct notes:", 2),
        ("task work:", 2),
        ("prompt work:", 2),
    ],
)
@pytest.mark.parametrize(
    "body",
    [
        "First.\n  Nested.\nLast.",
        "First.\n##! Literal heading.\ntools = prose.\n\nLast.",
        "First.\n\n\n\n\nLast.",
        "Here is an example: ```\n  content\n```\nLast.",
        "🧭 Review.\n  → Keep indentation.\n✓ Done.",
    ],
)
def test_text_consumers_share_the_formatting_contract(
    tab_size: int, header: str, depth: int, body: str
) -> None:
    source = (
        header
        + "\n"
        + "\n".join(" " * depth + line if line else "" for line in body.split("\n"))
        + "\n"
    )
    formatted = format_source(source, tab_size=tab_size)
    assert _semantics(formatted) == _semantics(source)
    assert format_source(formatted, tab_size=tab_size) == formatted


@pytest.mark.parametrize(
    "levels",
    [
        ("    ", "\t", "         "),
        ("\t", "         ", "\t\t"),
        ("  ", "    ", "      "),
        (" ", "  ", "   "),
    ],
)
@pytest.mark.parametrize("comment", ["# Comment.", "## Stage.", "##! Parent."])
def test_comments_do_not_change_nested_block_ownership(
    levels: tuple[str, str, str], comment: str
) -> None:
    first, second, third = levels
    for prefix in ("", *levels):
        for position in (1, 2, 3, 5):
            lines = [
                "flow work:",
                first + "repeat 2 times:",
                second + "repeat 1 time:",
                third + "run: Work.",
                second + "until: Done.",
                first + "run: Publish.",
            ]
            lines.insert(position, prefix + comment)
            source = "\n".join(lines) + "\n"
            formatted = format_source(source)
            assert _semantics(formatted) == _semantics(source), source
            assert format_source(formatted) == formatted, source
