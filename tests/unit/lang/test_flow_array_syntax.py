"""Strict runnable targets and removed statement diagnostics."""

import pytest

from toolang.lang import Program, format_source, format_statement_head
from toolang.lang.errors import ToolangError, ToolangFormatError


@pytest.mark.parametrize("operation", ["generate 2", "map", "reduce"])
@pytest.mark.parametrize("binding", ["", "let result = ", "let "])
@pytest.mark.parametrize("target", ["using worker", ": {{_}}", "-> Text: {{_}}"])
def test_collection_targets_follow_one_using_rule(operation, binding, target):
    source = (
        "agic worker(_: Text) -> Text:\n  {{_}}\n"
        f"flow main(_: Text[]):\n  {binding}{operation} {target}\n"
    )
    formatted = format_source(source)
    program = Program.from_source(formatted)
    assert format_source(formatted) == formatted
    head = format_statement_head(program.flows[0].stmts[0])
    assert ("using" in head) == target.startswith("using")


@pytest.mark.parametrize("operation", ["generate 2", "map", "reduce"])
@pytest.mark.parametrize("binding", ["", "let result = ", "let "])
@pytest.mark.parametrize("target", ["worker", "using: {{_}}", "using -> Text: {{_}}"])
def test_invalid_target_forms_are_rejected(operation, binding, target):
    source = f"flow main(_: Text[]):\n  {binding}{operation} {target}\n"
    for parse in (Program.from_source, format_source):
        with pytest.raises((ToolangError, ToolangFormatError)):
            parse(source)


@pytest.mark.parametrize("word", ["scatter", "gather", "storm", "settle"])
@pytest.mark.parametrize("binding", ["", "let value = "])
def test_retired_keywords_are_plain_text(word, binding):
    source = f"flow main:\n  {binding}{word} using worker\n"
    formatted = format_source(source)
    program = Program.from_source(formatted)
    assert program.flows[0].stmts[0].kind == ("let" if binding else "run")
    assert f"{word} using worker" in formatted
    assert format_source(formatted) == formatted


@pytest.mark.parametrize("operation", ["generate 2", "map"])
def test_lanes_precede_named_targets(operation):
    source = f"agic worker(_):\n  {{_}}\nflow main(_: Text[]):\n  {operation} in 2 lanes using worker\n"
    Program.from_source(source)
    with pytest.raises(ToolangError):
        Program.from_source(
            source.replace("in 2 lanes using worker", "using worker in 2 lanes")
        )
