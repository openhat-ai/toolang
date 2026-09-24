"""Flow descriptions depend only on the authored statement AST."""

import pytest

from toolang.lang.ast import (
    AskStmt,
    DropStmt,
    FlowStmt,
    GatherStmt,
    KeepStmt,
    LetStmt,
    MapStmt,
    Program,
    SortStmt,
    RepeatStmt,
    RunStmt,
    ScatterStmt,
    SeekStmt,
    SettleStmt,
    Span,
    StormStmt,
)

from toolang.lang.description import statement_description

SPAN = Span(line=1)


@pytest.mark.parametrize("doc", [None, "", " \n ", "Search the evidence"])
def test_automatic_statement_description_is_independent_of_docs(
    doc: str | None,
) -> None:
    statement = MapStmt(
        span=SPAN, runnable="agic:<adhoc:32>", lanes=2, binding="findings", doc=doc
    )
    description = (
        "Map each item with agic:<adhoc:32>, up to 2 at once, save result to findings"
    )

    assert statement_description(statement) == description


def test_statement_description_covers_inline_binding_and_repeat_forms() -> None:
    assert (
        statement_description(LetStmt(span=SPAN, binding="topic", value="x"))
        == "Set value to topic"
    )
    assert (
        statement_description(
            RunStmt(span=SPAN, runnable="agic:<adhoc:12>", binding=None)
        )
        == "Run agic:<adhoc:12>, discard result"
    )
    assert (
        statement_description(KeepStmt(span=SPAN, position="first", count=1))
        == "Keep the first item"
    )
    assert (
        statement_description(
            StormStmt(span=SPAN, count=3, runnable="review_item", lanes=2)
        )
        == "Storm into 3 items with review_item independently, up to 2 at once"
    )
    assert (
        statement_description(
            RepeatStmt(span=SPAN, count=3, runnable="agic:<adhoc:20>")
        )
        == "Repeat up to 3 times, until agic:<adhoc:20> is true"
    )


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        (RunStmt(span=SPAN, runnable="review_item"), "Run review_item"),
        (
            RunStmt(span=SPAN, runnable="review_item", binding="report"),
            "Run review_item, save result to report",
        ),
        (
            RunStmt(span=SPAN, runnable="review_item", binding=None),
            "Run review_item, discard result",
        ),
        (
            SeekStmt(span=SPAN, name="researcher", runnable="search_web"),
            "Ask agent researcher to run search_web",
        ),
        (
            SeekStmt(span=SPAN, name="researcher", runnable="agic:<adhoc:4>"),
            "Ask agent researcher to run agic:<adhoc:4>",
        ),
        (
            AskStmt(span=SPAN, name=None, request="question"),
            "Ask for human input",
        ),
        (
            AskStmt(span=SPAN, name="reviewer", request="question"),
            "Ask reviewer for input",
        ),
        (
            ScatterStmt(span=SPAN, runnable="expand_queries"),
            "Scatter into items with expand_queries",
        ),
        (
            ScatterStmt(span=SPAN, runnable="agic:<adhoc:5>"),
            "Scatter into items with agic:<adhoc:5>",
        ),
        (
            GatherStmt(span=SPAN, runnable="synthesize"),
            "Gather all items into one with synthesize",
        ),
        (
            GatherStmt(span=SPAN, runnable="agic:<adhoc:6>"),
            "Gather all items into one with agic:<adhoc:6>",
        ),
        (
            SettleStmt(span=SPAN, runnable="merge_pair"),
            "Settle all items into one with merge_pair sequentially",
        ),
        (
            MapStmt(span=SPAN, runnable="agic:<adhoc:7>"),
            "Map each item with agic:<adhoc:7>",
        ),
        (
            MapStmt(span=SPAN, runnable="search_web", lanes=1),
            "Map each item with search_web, one at a time",
        ),
        (
            KeepStmt(span=SPAN, runnable="is_relevant", lanes=3),
            "Keep items where is_relevant is true, up to 3 at once",
        ),
        (
            DropStmt(span=SPAN, position="last", count=2),
            "Drop the last 2 items",
        ),
        (
            DropStmt(span=SPAN, runnable="agic:<adhoc:8>"),
            "Drop items where agic:<adhoc:8> is true",
        ),
        (
            SortStmt(span=SPAN, runnable="agic:<adhoc:9>", order="ascending"),
            "Sort items by agic:<adhoc:9> in ascending order",
        ),
        (
            RepeatStmt(span=SPAN, count=2),
            "Repeat 2 times",
        ),
        (
            RepeatStmt(span=SPAN, count=1),
            "Repeat 1 time",
        ),
        (
            RepeatStmt(span=SPAN, count=1, runnable="complete"),
            "Repeat up to 1 time, until complete is true",
        ),
        (
            StormStmt(span=SPAN, count=1, runnable="review"),
            "Storm into 1 item with review independently",
        ),
        (
            RepeatStmt(span=SPAN, runnable="completion_check"),
            "Repeat until completion_check is true",
        ),
    ],
)
def test_statement_description_covers_every_statement(
    statement: FlowStmt,
    expected: str,
) -> None:
    assert statement_description(statement) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Review the findings.", "Run agic:<adhoc:3>"),
        ("run: Review the findings.", "Run agic:<adhoc:3>"),
        (
            "seek researcher: Find evidence.",
            "Ask agent researcher to run agic:<adhoc:3>",
        ),
        (
            "scatter using: Expand the query.",
            "Scatter into items with agic:<adhoc:3>",
        ),
        (
            "storm 3 in 2 lanes using: Review the findings.",
            "Storm into 3 items with agic:<adhoc:3> independently, up to 2 at once",
        ),
        (
            "gather using: Combine {{_}}.",
            "Gather all items into one with agic:<adhoc:3>",
        ),
        (
            "settle using: Merge {{_}}.",
            "Settle all items into one with agic:<adhoc:3> sequentially",
        ),
        (
            "let results = map in 2 lanes using:\n    Search for {{_}}.",
            "Map each item with agic:<adhoc:3>, up to 2 at once, save result to results",
        ),
        ("keep if: Check {{_}}.", "Keep items where agic:<adhoc:3> is true"),
        (
            "let drop if: Check {{_}}.",
            "Drop items where agic:<adhoc:3> is true, discard result",
        ),
        (
            "sort descending by: Score {{_}}.",
            "Sort items by agic:<adhoc:3> in descending order",
        ),
    ],
)
def test_descriptions_preserve_lowered_inline_runnable_names(
    source: str, expected: str
) -> None:
    program = Program.from_source(f"flow work:\n  scatter: Items\n  {source}\n")

    assert statement_description(program.flows[0].stmts[-1]) == expected
