"""Positional repeat syntax, flow analysis, and historical AST decoding."""

from typing import Any, cast

import pytest

from toolang.lang import Program, format_source, to_data
from toolang.lang.ast import (
    RepeatStmt,
    RunStmt,
    Span,
    flow_stmt_from_data,
    program_from_data,
)
from toolang.lang.description import statement_description
from toolang.lang.errors import ToolangError


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("condition", ["until ready", "until: Ready?"])
@pytest.mark.parametrize("count", ["", "0 times ", "3 times "])
@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_condition_position_survives_formatting_and_codecs(
    index, condition, count, ending
):
    lines = ["let first_value = First.", "let second_value = Second."]
    lines.insert(index, condition)
    source = (
        "agic ready() -> Boolean:\n  Ready?\nflow main():\n"
        f"  repeat {count}windowing 2:\n" + "".join(f"    {line}\n" for line in lines)
    ).replace("\n", ending)
    for text in (source, source.rstrip("\r\n"), source.replace("  ", "\t")):
        formatted = format_source(text)
        assert format_source(formatted) == formatted
        program = Program.from_source(formatted)
        loop = program.flows[0].stmts[0]
        assert isinstance(loop, RepeatStmt)
        assert loop.until_index == (None if index == 2 else index)
        assert [stmt.binding for stmt in loop.stmts] == ["first_value", "second_value"]
        assert loop.window == 2
        assert program_from_data(to_data(program)) == program
        assert flow_stmt_from_data(to_data(loop)) == loop
        if condition == "until ready":
            assert loop.runnable == "ready"
        else:
            assert loop.runnable is not None
            target = program.find_agic(loop.runnable)
            condition_line = next(
                number
                for number, line in enumerate(formatted.splitlines(), 1)
                if line.strip().startswith("until")
            )
            assert target is not None and target.span.line == condition_line
            assert target.output == "Boolean"
            assert target.directives[0].name == "tools"


def test_nested_conditions_and_docs_keep_their_owners():
    source = """flow main():
  repeat:
    ## Inner loop.
    repeat 2 times:
      until: Inner check.
      let value = Body.
    # Outer condition.
    until:
      Outer check.
      until here remains literal.
    ## Last statement.
    let result = Finished.
"""
    formatted = format_source(source)
    assert format_source(formatted) == formatted
    outer = Program.from_source(formatted).flows[0].stmts[0]
    assert isinstance(outer, RepeatStmt) and outer.until_index == 1
    inner = outer.stmts[0]
    assert isinstance(inner, RepeatStmt) and inner.until_index == 0
    assert inner.doc == "Inner loop."
    assert outer.stmts[1].doc == "Last statement."


@pytest.mark.parametrize("count", ["", "0 times ", "2 times "])
def test_repeat_without_condition(count):
    program = Program.from_source(
        f"flow main():\n  repeat {count}:\n    let value = Body.\n"
    )
    loop = program.flows[0].stmts[0]
    assert isinstance(loop, RepeatStmt) and loop.runnable is None
    assert loop.until_index is None
    if not count:
        assert statement_description(loop) == "Repeat indefinitely"


@pytest.mark.parametrize(
    "body",
    [
        "until: Done.",
        "until ready\n    let value = Body.\n    until: Done.",
        "until agic:ready\n    let value = Body.",
        "until checks::ready\n    let value = Body.",
        "let result = until ready\n    let value = Body.",
        "let until = value\n    let value = Body.",
    ],
)
def test_invalid_repeat_forms_are_rejected(body):
    with pytest.raises(ToolangError):
        Program.from_source(
            "agic ready() -> Boolean:\n  Ready.\nflow main():\n  repeat:\n    "
            + body
            + "\n"
        )


@pytest.mark.parametrize("condition", ["until ready", "until: {{value}}"])
def test_condition_inputs_are_checked_at_their_position(condition):
    header = "agic ready(value: Text) -> Boolean:\n  {{value}}\nflow main():\n  repeat 1 time:\n"
    with pytest.raises(ToolangError, match="Missing input 'value'"):
        Program.from_source(header + f"    {condition}\n    let value = Body.\n")
    Program.from_source(header + f"    let value = Body.\n    {condition}\n")


@pytest.mark.parametrize(
    "body",
    [
        "let value = Body.",
        "exec done\n    until: Done.",
    ],
)
def test_unbounded_or_transferring_prefix_has_no_fallthrough(body):
    Program.from_source(
        "flow done():\n  run: Done.\nflow main():\n  repeat:\n    "
        + body
        + "\n  run: {{missing}}\n"
    )


def test_condition_before_suffix_exec_preserves_fallthrough():
    with pytest.raises(ToolangError, match="Missing input 'missing'"):
        Program.from_source("""flow done():
  run: Done.
flow main():
  repeat:
    until: Done?
    exec done
  run: {{missing}}
""")


def test_condition_exit_joins_suffix_locals_conservatively():
    Program.from_source("""flow main():
  repeat:
    until: Done?
    let value = Body.
  run: {{value}}
""")


@pytest.mark.parametrize("target", ["ready", ": {{#_2}}true{{/_2}}"])
def test_zero_count_still_preflights_condition_history_window(target):
    with pytest.raises(ToolangError, match="outside the active window"):
        Program.from_source(
            "agic ready() -> Boolean:\n  {{#_2}}true{{/_2}}\nflow main():\n"
            f"  repeat 0 times windowing 1:\n    until {target}\n    let value = Body.\n"
        )


@pytest.mark.parametrize("index", [-1, 2, True, 0.5, "0"])
def test_invalid_condition_indices_are_rejected_by_both_codecs(index):
    loop = RepeatStmt(
        span=Span(1), runnable="ready", stmts=(RunStmt(span=Span(2), runnable="work"),)
    )
    data = cast(dict[str, Any], to_data(loop))
    data["until_index"] = index
    with pytest.raises(ValueError):
        flow_stmt_from_data(data)
    program = cast(
        dict[str, Any], to_data(Program.from_source("flow main():\n  run: Done.\n"))
    )
    program["flows"][0]["stmts"] = [data]
    with pytest.raises(ValueError):
        program_from_data(program)


def test_index_without_condition_is_rejected():
    with pytest.raises(ValueError, match="until_index"):
        RepeatStmt(span=Span(1), until_index=0)


def test_historical_nested_repeat_defaults_to_trailing_condition():
    inner = RepeatStmt(
        span=Span(2),
        count=1,
        runnable="ready",
        stmts=(RunStmt(span=Span(3), runnable="work"),),
    )
    outer = RepeatStmt(span=Span(1), count=2, stmts=(inner,))
    data = cast(dict[str, Any], to_data(outer))
    data.pop("until_index")
    data["stmts"][0].pop("until_index")
    assert flow_stmt_from_data(data) == outer


@pytest.mark.parametrize("name", ["value", "a0", "two_words", "a__b", "a_", "runtime"])
def test_regular_names_are_accepted(name):
    Program.from_source(f"flow main({name}: Text):\n  let {name} = Body.\n")


@pytest.mark.parametrize(
    "name", ["until", "repeat", "spawn", "run", "0value", "Upper", "a-b"]
)
@pytest.mark.parametrize("kind", ["parameter", "local"])
def test_invalid_variable_names_are_rejected(name, kind):
    source = (
        f"flow main({name}: Text):\n  run: Done.\n"
        if kind == "parameter"
        else f"flow main():\n  let {name} = Body.\n"
    )
    with pytest.raises(ToolangError):
        Program.from_source(source)
