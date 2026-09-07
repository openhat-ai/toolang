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

from toolang.lang.description import _scatter_description, statement_description

SPAN = Span(line=1)


@pytest.mark.parametrize("doc", [None, "", " \n ", "Search the evidence"])
def test_automatic_statement_description_is_independent_of_docs(
    doc: str | None,
) -> None:
    statement = MapStmt(
        span=SPAN, runnable="<agic:32>", lanes=2, binding="findings", doc=doc
    )
    description = (
        "Map each item with <agic:32>, up to 2 at once, save result to findings"
    )

    assert statement_description(statement) == description


@pytest.mark.parametrize(
    ("item_count", "expected"),
    [
        (None, "Scatter into items with expand"),
        (0, "Scatter into 0 items with expand"),
        (1, "Scatter into 1 item with expand"),
        (6, "Scatter into 6 items with expand"),
    ],
)
def test_scatter_description_supports_known_and_unknown_quantity(
    item_count: int | None, expected: str
) -> None:
    assert _scatter_description("expand", item_count) == expected


def test_statement_description_covers_inline_binding_and_repeat_forms() -> None:
    assert (
        statement_description(LetStmt(span=SPAN, binding="topic", value="x"))
        == "Set value to topic"
    )
    assert (
        statement_description(RunStmt(span=SPAN, runnable="<agic:12>", binding=None))
        == "Run <agic:12>, discard result"
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
        statement_description(RepeatStmt(span=SPAN, count=3, runnable="<agic:20>"))
        == "Repeat up to 3 times, until <agic:20> is true"
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
            SeekStmt(span=SPAN, name="researcher", runnable="<agic:4>"),
            "Ask agent researcher to run <agic:4>",
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
            ScatterStmt(span=SPAN, count=3, runnable="expand_queries"),
            "Scatter into 3 items with expand_queries",
        ),
        (
            ScatterStmt(span=SPAN, count=1, runnable="<agic:5>"),
            "Scatter into 1 item with <agic:5>",
        ),
        (
            GatherStmt(span=SPAN, runnable="synthesize"),
            "Gather all items into one with synthesize",
        ),
        (
            GatherStmt(span=SPAN, runnable="<agic:6>"),
            "Gather all items into one with <agic:6>",
        ),
        (
            SettleStmt(span=SPAN, runnable="merge_pair"),
            "Settle all items into one with merge_pair sequentially",
        ),
        (
            MapStmt(span=SPAN, runnable="<agic:7>"),
            "Map each item with <agic:7>",
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
            DropStmt(span=SPAN, runnable="<agic:8>"),
            "Drop items where <agic:8> is true",
        ),
        (
            SortStmt(span=SPAN, runnable="<agic:9>", order="ascending"),
            "Sort items by <agic:9> in ascending order",
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
        ("Review the findings.", "Run <agic:2>"),
        ("run: Review the findings.", "Run <agic:2>"),
        ("seek researcher: Find evidence.", "Ask agent researcher to run <agic:2>"),
        (
            "scatter 3 using: Expand the query.",
            "Scatter into 3 items with <agic:2>",
        ),
        (
            "storm 3 in 2 lanes using: Review the findings.",
            "Storm into 3 items with <agic:2> independently, up to 2 at once",
        ),
        (
            "gather using: Combine the findings.",
            "Gather all items into one with <agic:2>",
        ),
        (
            "settle using: Merge the next finding.",
            "Settle all items into one with <agic:2> sequentially",
        ),
        (
            "let results = map in 2 lanes using:\n    Search for evidence.",
            "Map each item with <agic:2>, up to 2 at once, save result to results",
        ),
        ("keep if: Check relevance.", "Keep items where <agic:2> is true"),
        (
            "let drop if: Check relevance.",
            "Drop items where <agic:2> is true, discard result",
        ),
        (
            "sort descending by: Score relevance.",
            "Sort items by <agic:2> in descending order",
        ),
    ],
)
def test_descriptions_preserve_lowered_inline_runnable_names(
    source: str, expected: str
) -> None:
    program = Program.from_source(f"flow work:\n  {source}\n")

    assert statement_description(program.flows[0].stmts[0]) == expected
