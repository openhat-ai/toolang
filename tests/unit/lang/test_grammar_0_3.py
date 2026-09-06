from dataclasses import replace
from typing import cast

import pytest

from toolang.common.errors import ToolangError
from toolang.lang import Program, format_source, format_statement_head, to_data
from toolang.lang.errors import ToolangFormatError
from toolang.lang.ast import (
    RepeatStmt,
    RunStmt,
    SortStmt,
    flow_stmt_from_data,
    program_from_data,
)


RUNNABLES = """agic worker -> Text:
  Work.
agic predicate -> Boolean:
  Decide.
agic score -> Number:
  Score.
"""


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("storm 3 in 2 lanes using worker", "storm 3 using worker in 2 lanes"),
        ("map in 2 lanes using worker", "map using worker in 2 lanes"),
        ("keep in 2 lanes if predicate", "keep if predicate in 2 lanes"),
        ("drop in 2 lanes if predicate", "drop if predicate in 2 lanes"),
        ("sort ascending in 2 lanes by score", "sort ascending by score in 2 lanes"),
    ],
)
def test_clause_order_preserves_semantics_and_lane_limit(
    first: str, second: str
) -> None:
    sources = [RUNNABLES + f"flow work:\n  {header}\n" for header in (first, second)]
    programs = [Program.from_source(source) for source in sources]
    assert programs[0] == programs[1]
    assert getattr(programs[0].flows[0].stmts[0], "lanes") == 2
    for source, header in zip(sources, (first, second), strict=True):
        formatted = format_source(source)
        assert header in formatted
        assert format_source(formatted) == formatted


@pytest.mark.parametrize(
    ("header", "output"),
    [
        ("scatter 2 using -> Text: Work.", "Text[]"),
        ("scatter 2 using: Work.", "Part[][]"),
        ("storm 2 in 1 lane using -> Text: Work.", "Text"),
        ("gather using: Work.", "Part[]"),
        ("settle using: Work with {{item}}.", "Part[]"),
        ("map using -> Text: Work.", "Text"),
        ("keep if: Decide.", "Boolean"),
        ("drop if -> Boolean: Decide.", "Boolean"),
        ("sort descending by: Score.", "Number"),
        ("sort ascending by -> Number: Score.", "Number"),
    ],
)
def test_inline_runnable_fields_preserve_the_operation_contract(
    header: str, output: str
) -> None:
    program = Program.from_source(f"flow work:\n  {header}\n")
    statement = program.flows[0].stmts[0]
    agic = program.find_agic(getattr(statement, "runnable"))
    assert agic is not None
    assert agic.output == output
    if header.startswith(("keep", "drop", "sort")):
        assert [(item.name, item.values) for item in agic.directives] == [
            ("recall", ("none",)),
            ("tools", ("*",)),
        ]
    assert program_from_data(to_data(program)) == program
    assert flow_stmt_from_data(to_data(statement)) == statement
    assert "<agic:" not in format_statement_head(statement)


@pytest.mark.parametrize(
    "header",
    [
        "map worker par 2",
        "map using worker in 0 lanes",
        "map in 1 lanes using worker",
        "map in 2 lane using worker",
        "map in 2 lanes in 3 lanes using worker",
        "map using worker using worker",
        "map using -> Text in 2 lanes: Work.",
        "map in 2 lanes with worker",
        "map using worker.",
        "sort by score",
        "sort by score descending",
        "rank score top 3",
        "par 2",
        "top 3",
        "bottom 3",
        "scatter 2: Work.",
        "storm {{n}} using worker",
        "repeat 2:\n    run worker",
        "repeat 1 times:\n    run worker",
        "repeat 2 time:\n    run worker",
        "repeat:\n    run worker",
        "until: Done.",
        "let result = repeat 2 times:\n    run worker",
        "let repeat 2 times:\n    run worker",
    ],
)
def test_invalid_and_legacy_headers_fail_before_lowering_or_execution(
    header: str,
) -> None:
    with pytest.raises(ToolangError, match="line"):
        Program.from_source(RUNNABLES + f"flow work:\n  {header}\n")


@pytest.mark.parametrize(
    "header",
    [
        "keep if -> Text: Decide.",
        "drop if -> Number: Decide.",
        "sort ascending by -> Boolean: Score.",
        "keep if worker",
        "drop if score",
        "sort descending by predicate",
    ],
)
def test_evaluator_return_types_are_not_silently_replaced(header: str) -> None:
    with pytest.raises(ToolangError, match=r"requires (Boolean|Number) output"):
        Program.from_source(RUNNABLES + f"flow work:\n  {header}\n")


def test_numeric_agreement_uses_value_and_zero_counts_remain_valid() -> None:
    program = Program.from_source(
        RUNNABLES
        + """flow work:
  storm 0 in 01 lane using worker
  keep first 0
  drop last 0
  repeat 01 time:
    repeat 0 times:
      run worker
"""
    )
    assert getattr(program.flows[0].stmts[0], "lanes") == 1
    repeat = program.flows[0].stmts[-1]
    assert isinstance(repeat, RepeatStmt)
    assert repeat.count == 1
    nested = repeat.stmts[0]
    assert isinstance(nested, RepeatStmt)
    assert nested.count == 0
    assert format_statement_head(repeat) == "repeat 1 time"


def test_sort_direction_and_binding_round_trip_as_canonical_step_data() -> None:
    program = Program.from_source(
        RUNNABLES + "flow work:\n  let sorted_items = sort descending by score\n"
    )
    statement = program.flows[0].stmts[0]
    assert isinstance(statement, SortStmt)
    assert statement.order == "descending"
    assert statement.binding == "sorted_items"
    data = cast(dict[str, object], to_data(statement))
    assert data["kind"] == "sort"
    assert flow_stmt_from_data(to_data(statement)) == statement
    assert "selection" not in data
    assert "limit" not in data
    assert format_statement_head(replace(statement, binding=None)) == (
        "let sort descending by score"
    )


def test_formatter_preserves_prose_continuations_and_statement_boundaries() -> None:
    source = """flow work:
  Explain the options.
  Sort     is a word in this prompt.

  map in 2 lanes using:
    Rewrite the option.

  Summarize the result.
"""
    before = Program.from_source(source)
    formatted = format_source(source)
    after = Program.from_source(formatted)
    assert [item.kind for item in before.flows[0].stmts] == ["run", "map", "run"]
    assert [item.messages for item in before.agics] == [
        item.messages for item in after.agics
    ]
    assert "Sort     is a word" in formatted
    assert format_source(formatted) == formatted


@pytest.mark.parametrize("indent", [" ", "  ", "\t"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("final_newline", [False, True])
def test_block_ownership_survives_lowering_and_formatting(
    indent: str, newline: str, final_newline: bool
) -> None:
    lines = [
        "flow work:",
        f"{indent}repeat:",
        "# Comments do not close the loop.",
        f"{indent * 2}## Improve the evidence.",
        f"{indent * 2}repeat 2 times:",
        f"{indent * 3}run:",
        f"{indent * 4}sort is literal prompt content.",
        f"{indent * 4}## A literal Markdown heading.",
        f"{indent * 5}- Keep this indentation.",
        f"{indent * 2}until:",
        f"{indent * 3}Return true when complete.",
        f"{indent}## Publish the result.",
        f"{indent}run: Publish the result.",
    ]
    source = newline.join(lines) + (newline if final_newline else "")
    formatted = format_source(source)
    assert format_source(formatted) == formatted
    for candidate in (source, formatted):
        program = Program.from_source(candidate)
        outer, publish = program.flows[0].stmts
        assert isinstance(outer, RepeatStmt)
        assert outer.count is None
        (inner,) = outer.stmts
        assert isinstance(inner, RepeatStmt)
        assert inner.doc == "Improve the evidence."
        assert inner.count == 2 and inner.runnable is None
        (run,) = inner.stmts
        assert isinstance(run, RunStmt)
        prompt = program.find_agic(run.runnable)
        assert prompt is not None
        content = prompt.messages[0].content
        assert content.splitlines()[:2] == [
            "sort is literal prompt content.",
            "## A literal Markdown heading.",
        ]
        assert content.splitlines()[2].lstrip() == "- Keep this indentation."
        assert content.splitlines()[2].startswith((" ", "\t"))
        assert outer.runnable is not None
        until = program.find_agic(outer.runnable)
        assert until is not None and until.output == "Boolean"
        assert until.messages[0].content == "Return true when complete."
        assert isinstance(publish, RunStmt)
        assert publish.doc == "Publish the result."
        publisher = program.find_agic(publish.runnable)
        assert publisher is not None
        assert publisher.messages[0].content == "Publish the result."


@pytest.mark.parametrize(
    "source",
    [
        "flow work:\n  repeat 2 times:\n    ## No body.\n  run: Publish.\n",
        "flow work:\n  repeat:\n    repeat 2 times:\n    until: Ready.\n",
        "flow work:\n  repeat 2 times:\n    run: Review.\n   until: Ready.\n",
        "flow work:\n  repeat 2 times:\n    run: Review.\n    until: Ready.\n    run: Again.\n",
        "flow work:\n  Review the evidence.\n  sort these items\n",
        "flow work:\n  Review the evidence.\n    run: Too deep.\n",
        "prompt example:\n\tdescription = Review.\n        Review the evidence.\n",
    ],
)
def test_parser_and_formatter_reject_invalid_block_structure(source: str) -> None:
    with pytest.raises(ToolangError):
        Program.from_source(source)
    with pytest.raises(ToolangFormatError):
        format_source(source)


@pytest.mark.parametrize("header", ["sort these items", "using worker", "run, extra"])
def test_syntax_errors_include_source_context_for_keyword_led_lines(
    header: str,
) -> None:
    source = f"flow work:\n  {header}\n"
    with pytest.raises(ToolangError, match="line 2") as parsed:
        Program.from_source(source)
    with pytest.raises(ToolangFormatError, match="line 2") as formatted:
        format_source(source)
    for error in (parsed.value, formatted.value):
        assert header in str(error)
        assert "Toolang 0.3 syntax" in str(error)
