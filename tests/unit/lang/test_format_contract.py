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
        if item.name is not None and item.name.startswith("<")
    }
    unnamed = {
        f"agic:<adhoc:{item.span.line}>": f"<adhoc:{index}>"
        for index, item in enumerate(program.agics)
        if item.name is None
    }
    names.update(unnamed)
    names.update({key.removeprefix("agic:"): value for key, value in unnamed.items()})

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


def test_prompt_declarations_preserve_literal_content_and_selections() -> None:
    source = """context notes: # Literal context follows.
  First.

  Second.
instruct rules: Keep this:
agic work:
  context = notes
  instruct = rules
  Work.
"""
    formatted = format_source(source)
    assert "  First.\n\n  Second." in formatted
    assert "instruct rules: Keep this:" in formatted
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
    if "map using:" in header:
        body += "\n{{_}}"
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


@pytest.mark.parametrize(
    "signature",
    ["_", "_: Part[]", "_: Text", "_, instruction", "instruction?: Text", ""],
)
def test_concise_signatures_preserve_authored_types_and_parameter_contract(signature):
    source = f"agic rewrite({signature}):\n    Rewrite.\n"
    formatted = format_source(source)
    assert formatted.startswith(f"agic rewrite({signature}):\n")
    assert _semantics(formatted) == _semantics(source)
    assert format_source(formatted) == formatted


def test_import_grouping_retains_source_order_and_documentation_barriers():
    source = (
        "with skill org/z\n\nwith skill org/a\nwith service org/b\n"
        "with skill org/c\n\n## Detached.\n\nwith skill org/d\n"
        "## Attached.\nwith skill org/e\n\nagic work:\n  Work.\n"
    )
    formatted = format_source(source)
    assert formatted.startswith(
        "with skill org/z\nwith skill org/a\n\nwith service org/b\n\nwith skill org/c\n"
    )
    assert "## Detached.\n\nwith skill org/d" in formatted
    assert "## Attached.\nwith skill org/e" in formatted
    assert _semantics(formatted) == _semantics(source)
    assert format_source(formatted) == formatted


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize("tab_size", [2, 4])
def test_directives_group_stably_by_first_key_without_blank_lines(
    kind: str, tab_size: int
) -> None:
    source = (
        f"{kind} work:\n"
        "  models = first\n\n"
        "  tools = fs/*  # Keep with tools.\n"
        "  models += second\n"
        "  skills = org/review\n"
        "  tools += shell/*\n"
        "  models -= third\n" + ("  Work.\n" if kind == "flow" else "  user: Work.\n")
    )
    indent = " " * tab_size
    expected = (
        f"{kind} work:\n"
        f"{indent}models = first\n"
        f"{indent}models += second\n"
        f"{indent}models -= third\n"
        f"{indent}tools = fs/*  # Keep with tools.\n"
        f"{indent}tools += shell/*\n"
        f"{indent}skills = org/review\n"
        + ("" if kind == "flow" else "\n")
        + f"{indent}{'Work.' if kind == 'flow' else 'user: Work.'}\n"
    )
    formatted = format_source(source, tab_size=tab_size)
    assert formatted == expected
    assert _directive_operations(formatted) == _directive_operations(source)
    assert format_source(formatted, tab_size=tab_size) == formatted


def _directive_operations(source: str) -> dict[str, list[tuple[str, tuple[str, ...]]]]:
    program = Program.from_source(source)
    runnable = program.flows[0] if program.flows else program.agics[0]
    operations: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for directive in runnable.directives:
        operations.setdefault(directive.name, []).append(
            (directive.operator, directive.values)
        )
    return operations


def test_directive_grouping_stops_at_comment_and_documentation_barriers() -> None:
    source = (
        "agic work:\n"
        "  models = first\n"
        "  tools = fs/*\n"
        "  # Plain barrier.\n"
        "  tools += shell/*\n"
        "  models += second\n"
        "  ## Message documentation.\n"
        "  user: Work.\n"
    )
    formatted = format_source(source)
    assert formatted == (
        "agic work:\n"
        "  models = first\n"
        "  tools = fs/*\n"
        "  # Plain barrier.\n\n"
        "  tools += shell/*\n"
        "  models += second\n"
        "  ## Message documentation.\n"
        "  user: Work.\n"
    )
    assert Program.from_source(formatted).agics[0].messages[0].doc == (
        "Message documentation."
    )
    assert _directive_operations(formatted) == _directive_operations(source)
    assert format_source(formatted) == formatted


@pytest.mark.parametrize(
    ("barrier", "separator", "expected_separator"),
    [
        ("# Plain barrier.", "", "\n"),
        ("## Documentation barrier.", "", ""),
        ("## Documentation barrier.", "\n", "\n"),
    ],
)
def test_directive_grouping_does_not_cross_comment_barriers(
    barrier: str,
    separator: str,
    expected_separator: str,
) -> None:
    source = (
        "agic work:\n"
        "  models = first\n"
        "  tools = fs/*\n"
        f"  {barrier}\n"
        f"{separator}"
        "  tools += shell/*\n"
        "  models += second\n"
        "  user: Work.\n"
    )
    formatted = format_source(source)
    assert formatted == (
        "agic work:\n"
        "  models = first\n"
        "  tools = fs/*\n"
        f"  {barrier}\n"
        f"{expected_separator}"
        "  tools += shell/*\n"
        "  models += second\n\n"
        "  user: Work.\n"
    )
    assert _directive_operations(formatted) == _directive_operations(source)
    assert format_source(formatted) == formatted


def test_directive_grouping_supports_every_current_agic_directive_key() -> None:
    source = (
        "agic work:\n"
        "  hands = agic:review\n"
        "  handoffs = flow:deliver\n"
        "  recall = near\n"
        "  models = first\n"
        "  tools = fs/*\n"
        "  models += second\n"
        "  psyches = org/calm\n"
        "  skills = org/review\n"
        "  tools += shell/*\n"
        "  services = org/search\n"
        "  user: Work.\n"
    )
    formatted = format_source(source)
    assert formatted == (
        "agic work:\n"
        "  hands = agic:review\n"
        "  handoffs = flow:deliver\n"
        "  recall = near\n"
        "  models = first\n"
        "  models += second\n"
        "  tools = fs/*\n"
        "  tools += shell/*\n"
        "  psyches = org/calm\n"
        "  skills = org/review\n"
        "  services = org/search\n\n"
        "  user: Work.\n"
    )
    assert _directive_operations(formatted) == _directive_operations(source)
    assert format_source(formatted) == formatted


def test_explicit_message_preserves_directive_looking_prose() -> None:
    source = "agic work:\n  models = first\n  user: prompts = literal prose.\n"
    formatted = format_source(source)
    assert "  user: prompts = literal prose.\n" in formatted
    agic = Program.from_source(formatted).agics[0]
    assert [directive.name for directive in agic.directives] == ["models"]
    assert agic.messages[0].content == "prompts = literal prose."
    assert format_source(formatted) == formatted


def test_directive_groups_keep_inline_conversations_compact() -> None:
    source = (
        "agic work:\n"
        "  models = first\n"
        "  tools = fs/*\n"
        "  models += second\n"
        "  user: First.\n\n"
        "  assistant: Second.\n\n"
        "  user: Third.\n"
    )
    formatted = format_source(source)
    assert formatted == (
        "agic work:\n"
        "  models = first\n"
        "  models += second\n"
        "  tools = fs/*\n\n"
        "  user: First.\n"
        "  assistant: Second.\n"
        "  user: Third.\n"
    )
    assert format_source(formatted) == formatted


def test_prose_flow_boundaries_keep_literal_body_whitespace():
    source = (
        "flow work:\n  Identify the question.\n  run:\n"
        "    First.\n\n\n    Second.\n  Write the answer.\n"
    )
    formatted = format_source(source)
    assert "Identify the question.\n\n  run:" in formatted
    assert "Second.\n\n  Write the answer." in formatted
    assert "First.\n\n\n    Second." in formatted
    assert _semantics(formatted) == _semantics(source)
    assert format_source(formatted) == formatted


@pytest.mark.parametrize("tab_size", [2, 4])
def test_prose_flow_spacing_respects_nested_statement_ownership(tab_size):
    source = (
        "flow work:\n"
        "  Start.\n"
        "  repeat 2 times:\n"
        "    First.\n"
        "    repeat 1 time:\n"
        "      Nested.\n"
        "    Second.\n"
        "    run:\n"
        "      Keep this.\n\n\n      And this.\n"
        "    Third.\n"
        "  Finish.\n"
    )
    expected = (
        "flow work:\n"
        "  Start.\n\n"
        "  repeat 2 times:\n"
        "    First.\n\n"
        "    repeat 1 time:\n"
        "      Nested.\n\n"
        "    Second.\n\n"
        "    run:\n"
        "      Keep this.\n\n\n      And this.\n\n"
        "    Third.\n\n"
        "  Finish.\n"
    )
    if tab_size == 4:
        expected = "\n".join(
            " " * (len(line) - len(line.lstrip())) + line
            for line in expected.split("\n")
        )
    formatted = format_source(source, tab_size=tab_size)
    assert formatted == expected
    assert _semantics(formatted) == _semantics(source)
    assert format_source(formatted, tab_size=tab_size) == formatted


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "flow work:\n  repeat 2 times:\n    run: Work.\n  Finish.\n",
            "flow work:\n  repeat 2 times:\n    run: Work.\n\n  Finish.\n",
        ),
        (
            "flow work:\n  repeat 2 times:\n    First.\n    until: Done?\n  Finish.\n",
            "flow work:\n"
            "  repeat 2 times:\n"
            "    First.\n"
            "    until: Done?\n\n"
            "  Finish.\n",
        ),
        (
            "flow work:\n"
            "  repeat 2 times:\n"
            "    repeat 1 time:\n"
            "      run: Work.\n"
            "    run: Publish.\n"
            "  Finish.\n",
            "flow work:\n"
            "  repeat 2 times:\n"
            "    repeat 1 time:\n"
            "      run: Work.\n"
            "    run: Publish.\n\n"
            "  Finish.\n",
        ),
    ],
)
def test_nested_flow_boundaries_cover_explicit_statements_and_until(
    source: str, expected: str
) -> None:
    formatted = format_source(source)
    assert formatted == expected
    assert _semantics(formatted) == _semantics(source)
    assert format_source(formatted) == formatted


@pytest.mark.parametrize("module_marker", ["#@", "##!"])
def test_new_documentation_conventions_preserve_parameter_bindings(module_marker):
    source = (
        f"#!/usr/bin/env too\n{module_marker}Module.\n"
        "##Rewrite text.\n## @param   _   Input.\n"
        "## @param instruction   Direction.\n## @return ordinary prose\n"
        "agic rewrite( _, instruction ? ) :\n"
        "    user: {{_}} {{instruction}}\n"
    )
    formatted = format_source(source)
    assert f"{module_marker} Module." in formatted
    assert "## @param _ Input.\n## @param instruction Direction." in formatted
    assert "agic rewrite(_, instruction?):" in formatted
    assert _semantics(formatted) == _semantics(source)
    assert format_source(formatted) == formatted


@pytest.mark.parametrize(
    "section", ["{{# _1 }}{{_1._}}{{/ _1 }}", "{{#.}}present{{/.}}"]
)
def test_template_sections_survive_lowering_and_formatting(section: str) -> None:
    body = "{{_}} " + section
    source = f"flow main:\n  repeat 2 times:\n    run: {body}\n"
    program = Program.from_source(source)
    assert program.agics[0].messages[0].content == body
    formatted = format_source(source)
    assert _semantics(formatted) == _semantics(source)
    assert format_source(formatted) == formatted
