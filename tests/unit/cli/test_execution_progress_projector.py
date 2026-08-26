from __future__ import annotations

import pytest

from toolang.base.types.message import (
    Part,
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.base.types.run import ModelCall, ToolCall
from toolang.cli.common.execution_progress import (
    ProgressProjector,
    ProgressBlock,
)
from toolang.cli.common.execution_progress.formatting import elapsed
from toolang.cli.common.execution_progress.headers import statement_header
from toolang.execution.events import (
    PartBegin,
    PartDelta,
    PartEnd,
    RunBegin,
    RunEnd,
    StepBegin,
    StepEnd,
)
from toolang.execution.types import (
    CollectionStepNoted,
    ControlRef,
    IterationOccurrence,
    Local,
    LoopStepNoted,
    LoopTermination,
    ModelStepGiven,
    ModelStepNoted,
    ModelTokenCount,
    Occurrence,
    OccurrencePosition,
    Pointer,
    StepPath,
    StepStatus,
    ToolStepGiven,
    ToolStepNoted,
    TypedPointer,
)
from toolang.lang.ast import (
    AskStmt,
    DropStmt,
    FlowStmt,
    GatherStmt,
    KeepStmt,
    LetStmt,
    MapStmt,
    RankStmt,
    RepeatStmt,
    RunStmt,
    ScatterStmt,
    SeekStmt,
    SettleStmt,
    Span,
    StormStmt,
)

SPAN = Span(line=1)


def _model(model: str = "deepseek/deepseek-chat") -> ModelStepGiven:
    return ModelStepGiven(model=model, call=ModelCall(instructions="", messages=[]))


def _tool(name: str = "web_search.search", *, summary: str = "") -> ToolStepGiven:
    return ToolStepGiven(
        plugin=name.split(".", 1)[0],
        call=ToolCall(
            tool_call_id="call_1",
            call_id="call_1",
            name=name,
            input={},
        ),
        summary=summary,
    )


def _parts(text: str) -> Local:
    return Local.typed("Part[]", (TextPart(text),), "_", 0)


def _part_output(*parts: Part) -> Local:
    return Local.typed("Part[]", parts, "_", 0)


def _tool_call_part() -> ToolCallPart:
    return ToolCallPart(
        tool_call_id="call_1",
        tool_name="web_search.search",
        tool_family="web_search",
        input={"query": "agent runtimes"},
    )


def _rows(blocks: tuple[ProgressBlock, ...]) -> list[list[str]]:
    return [[row.text for row in block.rows] for block in blocks]


def test_elapsed_normalizes_rounded_seconds_into_minutes() -> None:
    assert (
        elapsed(
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:01:59.600Z",
        )
        == "2m 00s"
    )


def test_progress_statement_header_prefers_doc_and_preserves_runnable_name() -> None:
    assert (
        statement_header(
            MapStmt(
                span=SPAN,
                runnable="search_web",
                lanes=4,
                doc="Search the web\nfor each query",
            )
        )
        == "Search the web for each query"
    )
    assert (
        statement_header(MapStmt(span=SPAN, runnable="search_web", lanes=4))
        == "Run search_web for each item, up to 4 at once"
    )
    assert (
        statement_header(
            RankStmt(
                span=SPAN,
                runnable="relevance_score",
                selection="top",
                limit=8,
                lanes=2,
                binding="findings",
            )
        )
        == "Rank items with relevance_score and keep the top 8 items, "
        "up to 2 at once and save as findings"
    )


def test_progress_statement_header_covers_inline_binding_and_repeat_forms() -> None:
    assert (
        statement_header(LetStmt(span=SPAN, binding="topic", value="x")) == "Set topic"
    )
    assert (
        statement_header(RunStmt(span=SPAN, runnable="<agic:12>", binding=None))
        == "Run the inline task without saving the result"
    )
    assert (
        statement_header(KeepStmt(span=SPAN, position="first", count=1))
        == "Keep the first item"
    )
    assert (
        statement_header(StormStmt(span=SPAN, count=3, runnable="review_item", lanes=2))
        == "Run review_item 3 times, up to 2 at once"
    )
    assert (
        statement_header(RepeatStmt(span=SPAN, count=3, runnable="<agic:20>"))
        == "Repeat up to 3 times"
    )


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        (RunStmt(span=SPAN, runnable="review_item"), "Run review_item"),
        (
            RunStmt(span=SPAN, runnable="review_item", binding="report"),
            "Run review_item and save as report",
        ),
        (
            RunStmt(span=SPAN, runnable="review_item", binding=None),
            "Run review_item without saving the result",
        ),
        (
            SeekStmt(span=SPAN, name="researcher", runnable="search_web"),
            "Ask researcher to run search_web",
        ),
        (
            SeekStmt(span=SPAN, name="researcher", runnable="<agic:4>"),
            "Ask researcher for help",
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
            "Expand into 3 items with expand_queries",
        ),
        (
            ScatterStmt(span=SPAN, count=1, runnable="<agic:5>"),
            "Expand into 1 item",
        ),
        (
            GatherStmt(span=SPAN, runnable="synthesize"),
            "Combine the items with synthesize",
        ),
        (
            GatherStmt(span=SPAN, runnable="<agic:6>"),
            "Combine the items",
        ),
        (
            SettleStmt(span=SPAN, runnable="merge_pair"),
            "Reduce the items with merge_pair",
        ),
        (
            MapStmt(span=SPAN, runnable="<agic:7>"),
            "Process each item",
        ),
        (
            KeepStmt(span=SPAN, runnable="is_relevant", lanes=3),
            "Keep items selected by is_relevant, up to 3 at once",
        ),
        (
            DropStmt(span=SPAN, position="last", count=2),
            "Drop the last 2 items",
        ),
        (
            DropStmt(span=SPAN, runnable="<agic:8>"),
            "Drop selected items",
        ),
        (
            RankStmt(span=SPAN, runnable="<agic:9>"),
            "Rank the items",
        ),
        (
            RepeatStmt(span=SPAN, count=2),
            "Repeat 2 times",
        ),
        (
            RepeatStmt(span=SPAN, runnable="completion_check"),
            "Repeat until complete",
        ),
    ],
)
def test_progress_statement_header_covers_every_ast_fallback(
    statement: FlowStmt,
    expected: str,
) -> None:
    assert statement_header(statement) == expected


@pytest.mark.parametrize(
    ("statement", "noted", "expected"),
    [
        (
            KeepStmt(span=SPAN, position="first", count=2),
            CollectionStepNoted(6, 2),
            "• Kept the first 2 items out of 6",
        ),
        (
            KeepStmt(span=SPAN, position="last", count=1),
            CollectionStepNoted(6, 1),
            "• Kept the last item out of 6",
        ),
        (
            KeepStmt(span=SPAN, position="first", count=8),
            CollectionStepNoted(6, 6),
            "• Kept all 6 items",
        ),
        (
            DropStmt(span=SPAN, position="first", count=2),
            CollectionStepNoted(6, 4),
            "• Dropped the first 2 items out of 6, leaving 4",
        ),
        (
            DropStmt(span=SPAN, position="last", count=8),
            CollectionStepNoted(6, 0),
            "• Dropped all 6 items, leaving none",
        ),
    ],
)
def test_positional_collection_steps_describe_the_transform(
    statement: KeepStmt | DropStmt,
    noted: CollectionStepNoted,
    expected: str,
) -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:demo",
        )
    )
    projector.handle(StepBegin(step=path, kind="value", given=statement))

    terminal = projector.handle(
        StepEnd(
            step=path,
            kind="value",
            status="succeeded",
            noted=noted,
        )
    )

    assert _rows(terminal.committed) == [[expected, ""]]
    assert terminal.committed[0].rows[0].tone == "normal"


@pytest.mark.parametrize(
    ("statement", "noted", "expected"),
    [
        (
            MapStmt(span=SPAN, runnable="map_item"),
            CollectionStepNoted(6, 6),
            "• Mapped all 6 items in parallel",
        ),
        (
            StormStmt(span=SPAN, count=6, runnable="brainstorm"),
            CollectionStepNoted(6, 6),
            "• Brainstormed 6 items in parallel",
        ),
        (
            KeepStmt(span=SPAN, runnable="accept"),
            CollectionStepNoted(6, 6),
            "• Evaluated 6 items in parallel, kept all 6",
        ),
        (
            DropStmt(span=SPAN, runnable="reject"),
            CollectionStepNoted(6, 4),
            "• Evaluated 6 items in parallel, dropped 2, leaving 4",
        ),
        (
            RankStmt(span=SPAN, runnable="score"),
            CollectionStepNoted(6, 6),
            "• Scored 6 items in parallel, ranked all 6",
        ),
        (
            RankStmt(
                span=SPAN,
                runnable="score",
                selection="top",
                limit=8,
            ),
            CollectionStepNoted(10, 8),
            "• Scored 10 items in parallel, kept the top 8",
        ),
    ],
)
def test_parallel_collection_steps_describe_execution_and_transform(
    statement: MapStmt | StormStmt | KeepStmt | DropStmt | RankStmt,
    noted: CollectionStepNoted,
    expected: str,
) -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:demo",
        )
    )
    live = projector.handle(StepBegin(step=path, kind="par", given=statement))

    assert live.live[0].rows[0].tone == "active"

    terminal = projector.handle(
        StepEnd(
            step=path,
            kind="par",
            status="succeeded",
            noted=noted,
        )
    )

    assert _rows(terminal.committed) == [[expected, ""]]
    assert terminal.committed[0].rows[0].tone == "normal"


def test_model_markdown_closes_without_repeating_at_step_end() -> None:
    reducer = ProgressProjector()
    reducer.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
            started_at="2026-01-01T00:00:00Z",
        )
    )
    live = reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.0"),
            kind="model",
            given=_model(),
            started_at="2026-01-01T00:00:00Z",
        )
    )
    assert _rows(live.live) == [["• Thinking..."]]
    assert live.live[0].gap_before is True

    reducer.handle(
        PartBegin(
            step=StepPath.parse("run_root.0"),
            part=0,
            part_type="text",
        )
    )
    streamed = reducer.handle(
        PartDelta(
            step=StepPath.parse("run_root.0"),
            part=0,
            delta=TextDelta(text="Comparing\napproaches"),
        )
    )
    assert _rows(streamed.live) == [["Comparing\napproaches"]]
    assert streamed.live[0].rows[0].format == "markdown"
    assert streamed.live[0].rows[0].prefix == "• "
    closed = reducer.handle(
        PartEnd(
            step=StepPath.parse("run_root.0"),
            part=0,
            data=TextPart("Comparing\napproaches"),
        )
    )
    assert _rows(closed.committed) == [["Comparing\napproaches"]]
    assert closed.committed[0].rows[0].format == "markdown"
    assert closed.live == ()

    final = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_root.0"),
            kind="model",
            status="succeeded",
            output=_parts("Comparing\napproaches"),
            noted=ModelStepNoted(
                tokens=ModelTokenCount(input=3400, output=86),
                cost="0.002",
            ),
            finished_at="2026-01-01T00:00:01.800Z",
        )
    )
    assert _rows(final.live) == []
    assert final.committed == ()
    assert reducer.needs_footer_gap


def test_model_markdown_commits_stable_blocks_and_keeps_one_live_tail() -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    projector.handle(StepBegin(step=path, kind="model", given=_model()))
    projector.handle(PartBegin(step=path, part=0, part_type="text"))

    heading = projector.handle(
        PartDelta(step=path, part=0, delta=TextDelta("# Heading\n\n"))
    )
    assert heading.committed == ()
    assert _rows(heading.live) == [["# Heading\n\n"]]
    assert heading.live[0].rows[0].prefix == "• "

    paragraph = projector.handle(
        PartDelta(step=path, part=0, delta=TextDelta("Paragraph"))
    )
    assert _rows(paragraph.committed) == [["# Heading\n\n"]]
    assert paragraph.committed[0].rows[0].prefix == "• "
    assert _rows(paragraph.live) == [["Paragraph"]]
    assert paragraph.live[0].rows[0].prefix == "  "
    assert paragraph.live[0].rows[0].gap_before is True

    closed = projector.handle(
        PartEnd(
            step=path,
            part=0,
            data=TextPart("# Heading\n\nParagraph"),
        )
    )
    assert _rows(closed.committed) == [["Paragraph"]]
    assert closed.committed[0].rows[0].prefix == "  "
    assert closed.committed[0].rows[0].gap_before is True
    assert closed.live == ()

    ended = projector.handle(
        StepEnd(
            step=path,
            kind="model",
            status="succeeded",
            output=_parts("# Heading\n\nParagraph"),
        )
    )
    assert ended.committed == ()
    assert ended.live == ()


def test_model_part_end_must_extend_streamed_text() -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    projector.handle(StepBegin(step=path, kind="model", given=_model()))
    projector.handle(PartBegin(step=path, part=0, part_type="text"))
    projector.handle(PartDelta(step=path, part=0, delta=TextDelta("prefix")))

    mismatch = projector.handle(PartEnd(step=path, part=0, data=TextPart("different")))

    assert mismatch.live == ()
    assert _rows(mismatch.committed) == [
        ["• PartEnd text does not extend TextDelta for run_root.0 part 0"]
    ]
    assert mismatch.committed[0].rows[0].tone == "error"


def test_model_step_end_must_repeat_the_completed_parts() -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    projector.handle(StepBegin(step=path, kind="model", given=_model()))
    projector.handle(PartBegin(step=path, part=0, part_type="text"))
    projector.handle(PartEnd(step=path, part=0, data=TextPart("authoritative")))

    mismatch = projector.handle(
        StepEnd(
            step=path,
            kind="model",
            status="succeeded",
            output=_parts("different"),
        )
    )

    assert mismatch.live == ()
    assert _rows(mismatch.committed) == [
        ["• StepEnd output does not match completed Parts for run_root.0"]
    ]
    assert mismatch.committed[0].rows[0].tone == "error"


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        ("failed", "stream disconnected", "• failed stream disconnected"),
        ("canceled", None, "• canceled"),
    ],
)
def test_partial_model_output_is_committed_before_terminal_failure(
    status: StepStatus,
    error: str | None,
    expected: str,
) -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    projector.handle(StepBegin(step=path, kind="model", given=_model()))
    projector.handle(PartBegin(step=path, part=0, part_type="text"))
    projector.handle(PartDelta(step=path, part=0, delta=TextDelta("partial")))
    closed = projector.handle(PartEnd(step=path, part=0, data=TextPart("partial")))
    assert _rows(closed.committed) == [["partial"]]

    ended = projector.handle(
        StepEnd(
            step=path,
            kind="model",
            status=status,
            error=error,
        )
    )

    assert _rows(ended.committed) == [[expected]]
    assert ended.committed[0].rows[0].tone == ("error" if error else "warning")


def test_model_tool_call_only_output_discards_live_presentation() -> None:
    reducer = ProgressProjector(show_boundaries=False)
    reducer.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.0"),
            kind="model",
            given=_model(),
        )
    )

    update = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_root.0"),
            kind="model",
            status="succeeded",
            output=_part_output(_tool_call_part()),
            noted=ModelStepNoted(
                tokens=ModelTokenCount(input=12, output=3),
                cost="0.001",
            ),
        )
    )

    assert update.committed == ()
    assert update.live == ()
    assert reducer._steps == {}
    assert reducer.root_metrics.model_calls == 1
    assert reducer.root_metrics.input_tokens == 12
    assert reducer.root_metrics.output_tokens == 3


def test_model_mixed_output_hides_tool_calls_and_keeps_visible_parts() -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    projector.handle(StepBegin(step=path, kind="model", given=_model()))

    update = projector.handle(
        StepEnd(
            step=path,
            kind="model",
            status="succeeded",
            output=_part_output(
                _tool_call_part(),
                TextPart("I will search for current evidence."),
            ),
        )
    )

    assert _rows(update.committed) == [["• I will search for current evidence."]]
    assert "web_search" not in update.committed[0].rows[0].text


def test_model_committed_text_is_not_replaced_by_later_tool_call_part() -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    projector.handle(StepBegin(step=path, kind="model", given=_model()))
    projector.handle(PartBegin(step=path, part=0, part_type="text"))
    visible = projector.handle(
        PartEnd(step=path, part=0, data=TextPart("Visible reasoning."))
    )
    projector.handle(PartBegin(step=path, part=1, part_type="tool_call"))
    hidden = projector.handle(PartEnd(step=path, part=1, data=_tool_call_part()))

    terminal = projector.handle(
        StepEnd(
            step=path,
            kind="model",
            status="succeeded",
            output=_part_output(TextPart("Visible reasoning."), _tool_call_part()),
        )
    )

    assert _rows(visible.committed) == [["Visible reasoning."]]
    assert hidden.committed == ()
    assert hidden.live == ()
    assert terminal.committed == ()
    assert terminal.live == ()


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        ("failed", "provider unavailable", "• failed provider unavailable"),
        ("canceled", None, "• canceled"),
    ],
)
def test_model_tool_call_output_keeps_failure_diagnostics(
    status: StepStatus,
    error: str | None,
    expected: str,
) -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    projector.handle(StepBegin(step=path, kind="model", given=_model()))

    update = projector.handle(
        StepEnd(
            step=path,
            kind="model",
            status=status,
            output=_part_output(_tool_call_part()),
            error=error,
        )
    )

    assert _rows(update.committed) == [[expected]]


def test_model_empty_success_keeps_completed_fallback() -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    projector.handle(StepBegin(step=path, kind="model", given=_model()))

    update = projector.handle(
        StepEnd(
            step=path,
            kind="model",
            status="succeeded",
            output=_part_output(),
        )
    )

    assert _rows(update.committed) == [["• completed"]]


def test_parallel_lane_tool_call_only_model_hands_activity_to_tool_step() -> None:
    projector = ProgressProjector(show_boundaries=False)
    par = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:demo",
        )
    )
    projector.handle(
        StepBegin(
            step=par,
            kind="par",
            given=MapStmt(span=SPAN, runnable="research", lanes=1),
        )
    )
    projector.handle(
        RunBegin(
            run="run_child",
            parent=par,
            control=ControlRef("run_child", 0),
            runnable="agic:research",
            occurrence=Occurrence(
                item=OccurrencePosition(index=0, count=1),
                lane=OccurrencePosition(index=0, count=1),
            ),
        )
    )
    model = StepPath.parse("run_child.0")
    projector.handle(StepBegin(step=model, kind="model", given=_model()))

    hidden = projector.handle(
        StepEnd(
            step=model,
            kind="model",
            status="succeeded",
            output=_part_output(_tool_call_part()),
        )
    )

    assert hidden.committed == ()
    assert "requested" not in " ".join(row.text for row in hidden.live[0].rows)
    assert "web_search.search" not in " ".join(row.text for row in hidden.live[0].rows)

    tool = projector.handle(
        StepBegin(
            step=StepPath.parse("run_child.1"),
            kind="tool",
            given=_tool(),
        )
    )

    assert _rows(tool.live) == [
        [
            "• running · 0/1 succeeded · 1 active",
            "  0 | #0 | • executing web_search.search",
        ]
    ]


def test_flow_run_header_wraps_real_agic_steps_without_a_wrapper_row() -> None:
    reducer = ProgressProjector()
    reducer.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:research",
        )
    )
    header = reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.0"),
            kind="run",
            given=RunStmt(span=SPAN, runnable="summarize"),
            started_at="2026-01-01T00:00:00Z",
        )
    )
    assert _rows(header.committed) == [["[0] Run summarize", ""]]
    reducer.handle(
        RunBegin(
            run="run_child",
            parent=StepPath.parse("run_root.0"),
            control=ControlRef("run_child", 0),
            runnable="agic:summarize",
        )
    )
    live = reducer.handle(
        StepBegin(
            step=StepPath.parse("run_child.0"),
            kind="model",
            given=_model(),
        )
    )
    assert _rows(live.live) == [["• Thinking..."]]
    finalized = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_child.0"),
            kind="model",
            status="succeeded",
            output=_parts("Done."),
            noted=ModelStepNoted(
                tokens=ModelTokenCount(input=639, output=215),
                cost="0.00149",
            ),
        )
    )
    assert _rows(finalized.committed) == [["• Done."]]
    reducer.handle(RunEnd(run="run_child", status="succeeded"))
    wrapper = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_root.0"),
            kind="run",
            status="succeeded",
            output=_parts("Done."),
            finished_at="2026-01-01T00:00:02Z",
        )
    )
    assert _rows(wrapper.committed) == [
        [
            "  2.0s · 1 run · 1 model call · ↑639 ↓215 ~$0.00149",
            "",
        ]
    ]
    assert wrapper.committed[0].gap_before is False
    assert wrapper.committed[0].rows[0].right_text == "run_root.0"


@pytest.mark.parametrize(
    ("output", "expected_output"),
    [
        (
            {"results": [{}, {}, {}, {}, {}]},
            ['  {"results":[{},{},{},{},{}]}'],
        ),
        (
            {"stdout": "first line\nsecond line\n", "exit_code": 0},
            ["  first line", "  second line"],
        ),
    ],
)
def test_tool_output_uses_compact_json_and_preserves_text_lines(
    output: dict[str, object],
    expected_output: list[str],
) -> None:
    reducer = ProgressProjector(show_boundaries=False)
    reducer.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    live = reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.0"),
            kind="tool",
            given=_tool(),
        )
    )
    assert live.live[0].rows[0].tone == "active"
    assert live.live[0].rows[0].surface == "tool_summary"
    update = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_root.0"),
            kind="tool",
            status="succeeded",
            output=Local.typed(
                "Part[]",
                (
                    ToolResultPart(
                        tool_call_id="call_1",
                        tool_name="web_search.search",
                        tool_family="web_search",
                        output=output,
                    ),
                ),
                "_",
                0,
            ),
        )
    )
    assert _rows(update.committed) == [
        ["• executed web_search.search", *expected_output]
    ]
    assert all(row.tone == "progress" for row in update.committed[0].rows)
    assert update.committed[0].rows[0].surface == "tool_summary"
    assert all(row.surface == "tool_detail" for row in update.committed[0].rows[1:])


def test_tool_error_preserves_complete_multiline_output() -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    live = projector.handle(
        StepBegin(
            step=path,
            kind="tool",
            given=_tool(summary="calling web search for Toolang"),
        )
    )
    assert _rows(live.live) == [["• calling web search for Toolang"]]

    terminal = projector.handle(
        StepEnd(
            step=path,
            kind="tool",
            status="failed",
            noted=ToolStepNoted(summary="failed to call web search for Toolang"),
            error="provider rejected the request\nretry after 60 seconds",
        )
    )

    assert _rows(terminal.committed) == [
        [
            "• failed to call web search for Toolang",
            "  provider rejected the request",
            "  retry after 60 seconds",
        ]
    ]
    assert [row.surface for row in terminal.committed[0].rows] == [
        "tool_summary",
        "tool_detail",
        "tool_detail",
    ]


def test_canceled_tool_summary_is_separated_but_error_remains_plain() -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    projector.handle(
        StepBegin(
            step=path,
            kind="tool",
            given=_tool(summary="calling web search for Toolang"),
        )
    )

    terminal = projector.handle(
        StepEnd(
            step=path,
            kind="tool",
            status="canceled",
            noted=ToolStepNoted(summary="canceled web search for Toolang"),
            error="interrupted by user",
        )
    )

    assert _rows(terminal.committed) == [
        ["• canceled web search for Toolang", "  interrupted by user"]
    ]
    assert [row.surface for row in terminal.committed[0].rows] == [
        "tool_summary",
        "none",
    ]


def test_flow_scalar_output_is_displayed_in_its_normal_output_slot() -> None:
    reducer = ProgressProjector(show_boundaries=False)
    reducer.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:demo",
        )
    )
    reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.0"),
            kind="value",
            given=LetStmt(span=SPAN, binding="topic", value="agent runtimes"),
        )
    )
    update = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_root.0"),
            kind="value",
            status="succeeded",
            output=Local.typed("Text", "agent runtimes", "topic", 0),
        )
    )

    assert _rows(update.committed) == [["• agent runtimes", ""]]
    assert update.committed[0].rows[0].tone == "normal"
    assert all(not row.right_text for row in update.committed[0].rows)
    assert not reducer.needs_footer_gap


def test_flow_list_output_uses_presentation_data_without_storage_tags() -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:demo",
        )
    )
    projector.handle(
        StepBegin(
            step=path,
            kind="value",
            given=LetStmt(span=SPAN, binding="queries", value="[]"),
        )
    )

    terminal = projector.handle(
        StepEnd(
            step=path,
            kind="value",
            status="succeeded",
            output=Local.typed(
                "Text[]",
                ("query one", "query two"),
                "queries",
                1,
            ),
        )
    )

    assert _rows(terminal.committed) == [['• ["query one","query two"]', ""]]


def test_flow_pointer_backed_output_is_not_displayed() -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:demo",
        )
    )
    projector.handle(
        StepBegin(
            step=path,
            kind="value",
            given=LetStmt(span=SPAN, binding="topic", value="source"),
        )
    )

    terminal = projector.handle(
        StepEnd(
            step=path,
            kind="value",
            status="succeeded",
            output=Local.typed(
                "Text",
                TypedPointer(
                    "Text",
                    Pointer.step(StepPath.parse("run_source.0")),
                ),
                "topic",
                0,
            ),
        )
    )

    assert terminal.committed == ()


def test_repeat_uses_flat_iteration_and_statement_boundaries() -> None:
    reducer = ProgressProjector()
    reducer.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:work",
        )
    )
    repeat_header = reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.2"),
            kind="loop",
            given=RepeatStmt(span=SPAN, count=3, runnable="completion_check"),
        )
    )
    assert _rows(repeat_header.committed) == [["[2] Repeat up to 3 times", ""]]
    iteration_header = reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.2.0"),
            kind="run",
            given=RunStmt(span=SPAN, runnable="review"),
            occurrence=Occurrence(
                iteration=IterationOccurrence(index=0, count=3, phase="body")
            ),
        )
    )
    assert _rows(iteration_header.committed) == [
        ["--- iteration 1 of 3 ---", "", "[0] Run review", ""]
    ]
    reducer.handle(
        RunBegin(
            run="run_review",
            parent=StepPath.parse("run_root.2.0"),
            control=ControlRef("run_review", 0),
            runnable="agic:review",
            occurrence=Occurrence(
                iteration=IterationOccurrence(index=0, count=3, phase="body")
            ),
        )
    )
    live = reducer.handle(
        StepBegin(
            step=StepPath.parse("run_review.0"),
            kind="model",
            given=_model(),
        )
    )
    assert _rows(live.live) == [["• Thinking..."]]
    reducer.handle(
        StepEnd(
            step=StepPath.parse("run_review.0"),
            kind="model",
            status="succeeded",
            output=_parts("revised"),
        )
    )
    reducer.handle(RunEnd(run="run_review", status="succeeded"))
    reducer.handle(
        StepEnd(
            step=StepPath.parse("run_root.2.0"),
            kind="run",
            status="succeeded",
            output=_parts("revised"),
        )
    )
    completed = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_root.2"),
            kind="loop",
            status="succeeded",
            output=_parts("revised"),
            noted=LoopStepNoted(iterations=1, termination="exhausted"),
        )
    )

    assert _rows(completed.committed) == [
        [
            "• Completed 1 iteration without meeting the condition",
            "  1 run · 1 model call",
            "",
        ]
    ]


def test_until_run_shows_control_boundary_and_only_real_agic_steps() -> None:
    reducer = ProgressProjector()
    reducer.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:work",
        )
    )
    repeat_header = reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.0"),
            kind="loop",
            given=RepeatStmt(span=SPAN, count=3, runnable="completion_check"),
        )
    )
    assert _rows(repeat_header.committed) == [["[0] Repeat up to 3 times", ""]]
    reducer.handle(
        RunBegin(
            run="run_until",
            parent=StepPath.parse("run_root.0"),
            control=ControlRef("run_until", 0),
            runnable="agic:completion_check",
            occurrence=Occurrence(
                iteration=IterationOccurrence(index=0, count=3, phase="until")
            ),
        )
    )
    live = reducer.handle(
        StepBegin(
            step=StepPath.parse("run_until.0"),
            kind="model",
            given=_model(),
        )
    )
    assert _rows(live.committed) == [["<?> completion_check", ""]]
    assert _rows(live.live) == [["• Thinking..."]]
    final = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_until.0"),
            kind="model",
            status="succeeded",
            output=_parts("true"),
        )
    )
    text = "\n".join(_rows(final.committed)[0])
    assert "• true" in text
    assert "executed completion_check" not in text


def test_parallel_lane_is_single_line_and_terminal_failure_replaces_lanes() -> None:
    reducer = ProgressProjector(show_boundaries=False)
    reducer.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:work",
        )
    )
    reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.0"),
            kind="par",
            given=MapStmt(span=SPAN, runnable="search_web", lanes=2),
        )
    )
    for item in range(2):
        reducer.handle(
            RunBegin(
                run=f"run_child_{item}",
                parent=StepPath.parse("run_root.0"),
                control=ControlRef(f"run_child_{item}", 0),
                runnable="agic:search_web",
                occurrence=Occurrence(
                    item=OccurrencePosition(index=item + 4, count=8),
                    lane=OccurrencePosition(index=item, count=2),
                ),
            )
        )
        reducer.handle(
            StepBegin(
                step=StepPath.parse(f"run_child_{item}.0"),
                kind="tool" if item == 0 else "model",
                given=_tool("fetch_page") if item == 0 else _model(),
            )
        )

    model = StepPath.parse("run_child_1.0")
    reducer.handle(PartBegin(step=model, part=0, part_type="text"))
    streamed = reducer.handle(
        PartDelta(
            step=model,
            part=0,
            delta=TextDelta("first lane line\nsecond lane line"),
        )
    )
    assert _rows(streamed.live) == [
        [
            "• running · 0/8 succeeded · 2 active",
            "  0 | #4 | • executing fetch_page",
            "  1 | #5 | • first lane line second lane line",
        ]
    ]

    failed = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_child_0.0"),
            kind="tool",
            status="failed",
            error="provider returned status 429",
        )
    )
    assert _rows(failed.live) == [
        [
            "• running · 0/8 succeeded · 2 active",
            "  0 | #4 | • failed fetch_page · provider returned status 429",
            "  1 | #5 | • first lane line second lane line",
        ]
    ]
    child_failed = reducer.handle(
        RunEnd(
            run="run_child_0",
            status="failed",
            error=Pointer.step(StepPath.parse("run_child_0.0")),
        )
    )
    assert _rows(child_failed.live) == [
        [
            "• running · 0/8 succeeded · 1 failed · 1 canceling",
            "  0 | #4 | • failed fetch_page · provider returned status 429",
            "  1 | #5 | • canceling",
        ]
    ]
    reducer.handle(
        PartEnd(
            step=model,
            part=0,
            data=TextPart("first lane line\nsecond lane line"),
        )
    )
    reducer.handle(
        StepEnd(
            step=model,
            kind="model",
            status="canceled",
        )
    )
    reducer.handle(RunEnd(run="run_child_1", status="canceled"))
    terminal = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_root.0"),
            kind="par",
            status="failed",
            error="parallel step stopped because lane 0 (#4) failed",
        )
    )
    assert terminal.live == ()
    assert _rows(terminal.committed) == [
        [
            "• Parallel execution stopped: 0/8 succeeded, 1 failed, and 1 was canceled",
            "  0 | #4 | • failed fetch_page",
            "             provider returned status 429",
            "",
            "• parallel step stopped because lane 0 (#4) failed",
            "  2 runs · 1 model call · 1 tool call",
            "",
        ]
    ]
    assert terminal.committed[0].rows[-2].right_text == "run_root.0"


def test_parent_error_pointers_are_silent_but_ownerless_run_errors_are_visible() -> (
    None
):
    reducer = ProgressProjector(show_boundaries=False)
    reducer.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.0"),
            kind="tool",
            given=_tool("fetch_page"),
        )
    )
    leaf = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_root.0"),
            kind="tool",
            status="failed",
            error="connection closed",
        )
    )
    assert "connection closed" in "\n".join(_rows(leaf.committed)[0])
    root = reducer.handle(
        RunEnd(
            run="run_root",
            status="failed",
            error=Pointer.step(StepPath.parse("run_root.0")),
        )
    )
    assert root.committed == ()

    ownerless = ProgressProjector()
    ownerless.handle(
        RunBegin(
            run="run_other",
            control=ControlRef("run_other", 0),
            runnable="agic:demo",
        )
    )
    update = ownerless.handle(
        RunEnd(
            run="run_other",
            status="failed",
            error="progress stream ended before run completion",
        )
    )
    assert _rows(update.committed) == [
        ["• progress stream ended before run completion"]
    ]


def test_malformed_part_sequence_becomes_one_root_diagnostic() -> None:
    reducer = ProgressProjector()
    reducer.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.0"),
            kind="model",
            given=_model(),
        )
    )
    update = reducer.handle(
        PartDelta(
            step=StepPath.parse("run_root.0"),
            part=0,
            delta=TextDelta(text="bad"),
        )
    )
    assert update.live == ()
    assert "PartDelta without active Part" in _rows(update.committed)[0][0]


def test_terminal_diagnostic_preserves_the_active_statement_header() -> None:
    projector = ProgressProjector()
    flow = StepPath.parse("run_root.0")
    model = StepPath.parse("run_child.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:research",
        )
    )
    projector.handle(
        StepBegin(
            step=flow,
            kind="run",
            given=RunStmt(
                span=SPAN,
                runnable="synthesize",
                doc="Synthesize the final research brief",
            ),
        )
    )
    projector.handle(
        RunBegin(
            run="run_child",
            parent=flow,
            control=ControlRef("run_child", 0),
            runnable="agic:synthesize",
        )
    )
    projector.handle(StepBegin(step=model, kind="model", given=_model()))
    projector.handle(PartBegin(step=model, part=0, part_type="text"))

    update = projector.handle(StepEnd(step=model, kind="model", status="canceled"))

    assert _rows(update.committed) == [["• StepEnd with active Part for run_child.0"]]
    assert update.live == ()


def test_parallel_lane_cannot_be_reused_while_its_run_is_active() -> None:
    projector = ProgressProjector(show_boundaries=False)
    par = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:demo",
        )
    )
    projector.handle(
        StepBegin(
            step=par,
            kind="par",
            given=MapStmt(span=SPAN, runnable="work", lanes=1),
        )
    )
    occurrence = Occurrence(
        item=OccurrencePosition(index=0, count=2),
        lane=OccurrencePosition(index=0, count=1),
    )
    projector.handle(
        RunBegin(
            run="run_first",
            parent=par,
            control=ControlRef("run_first", 0),
            runnable="agic:work",
            occurrence=occurrence,
        )
    )

    duplicate = projector.handle(
        RunBegin(
            run="run_second",
            parent=par,
            control=ControlRef("run_second", 0),
            runnable="agic:work",
            occurrence=occurrence,
        )
    )

    assert duplicate.live == ()
    assert _rows(duplicate.committed) == [
        ["• parallel lane 0 already owns active Run run_first"]
    ]


def test_run_end_with_active_step_clears_live_with_one_diagnostic() -> None:
    reducer = ProgressProjector()
    reducer.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.0"),
            kind="model",
            given=_model(),
        )
    )

    update = reducer.handle(RunEnd(run="run_root", status="canceled"))

    assert update.live == ()
    assert _rows(update.committed) == [["• RunEnd with active Step for run_root"]]


def test_nested_cancellation_is_rendered_once_at_the_leaf() -> None:
    projector = ProgressProjector()
    flow = StepPath.parse("run_root.0")
    model = StepPath.parse("run_child.0")
    finalized: list[ProgressBlock] = []

    events = (
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:research",
        ),
        StepBegin(
            step=flow,
            kind="run",
            given=ScatterStmt(
                span=SPAN,
                count=6,
                runnable="expand_queries",
                doc="Expand the research question into diverse search queries",
            ),
            started_at="2026-01-01T00:00:00Z",
        ),
        RunBegin(
            run="run_child",
            parent=flow,
            control=ControlRef("run_child", 0),
            runnable="agic:expand_queries",
        ),
        StepBegin(step=model, kind="model", given=_model()),
        StepEnd(step=model, kind="model", status="canceled"),
        RunEnd(run="run_child", status="canceled", error="canceled"),
        StepEnd(
            step=flow,
            kind="run",
            status="canceled",
            finished_at="2026-01-01T00:00:02Z",
        ),
        RunEnd(run="run_root", status="canceled", error="script interrupted"),
    )
    for event in events:
        finalized.extend(projector.handle(event).committed)

    lines = [row.text for block in finalized for row in block.rows]

    assert lines == [
        "[0] Expand the research question into diverse search queries",
        "",
        "• canceled",
        "  2.0s · 1 run · 1 model call",
        "",
        "• script interrupted",
    ]


def test_cyclic_error_pointer_becomes_one_terminal_diagnostic() -> None:
    reducer = ProgressProjector(show_boundaries=False)
    reducer.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    for index in range(2):
        reducer.handle(
            StepBegin(
                step=StepPath.parse(f"run_root.{index}"),
                kind="model",
                given=_model(),
            )
        )
    reducer.handle(
        StepEnd(
            step=StepPath.parse("run_root.0"),
            kind="model",
            status="failed",
            error=Pointer.step(StepPath.parse("run_root.1")),
        )
    )
    reducer.handle(
        StepEnd(
            step=StepPath.parse("run_root.1"),
            kind="model",
            status="failed",
            error=Pointer.step(StepPath.parse("run_root.0")),
        )
    )

    update = reducer.handle(
        RunEnd(
            run="run_root",
            status="failed",
            error=Pointer.step(StepPath.parse("run_root.0")),
        )
    )

    assert _rows(update.committed) == [
        ["• could not resolve execution error run_root.0"]
    ]


def test_compact_mode_removes_repeat_boundaries_but_keeps_agic_activity() -> None:
    reducer = ProgressProjector(show_boundaries=False)
    reducer.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:work",
        )
    )
    reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.0"),
            kind="loop",
            given=RepeatStmt(span=SPAN, count=2),
        )
    )
    reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.0.0"),
            kind="run",
            given=RunStmt(span=SPAN, runnable="review"),
            occurrence=Occurrence(
                iteration=IterationOccurrence(index=0, count=2, phase="body")
            ),
        )
    )
    reducer.handle(
        RunBegin(
            run="run_review",
            parent=StepPath.parse("run_root.0.0"),
            control=ControlRef("run_review", 0),
            runnable="agic:review",
        )
    )
    live = reducer.handle(
        StepBegin(
            step=StepPath.parse("run_review.0"),
            kind="model",
            given=_model(),
        )
    )

    assert _rows(live.live) == [["• Thinking..."]]


def test_nested_flow_inside_parallel_stays_in_one_reusable_lane() -> None:
    reducer = ProgressProjector(show_boundaries=False)
    par = StepPath.parse("run_root.0")
    reducer.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:work",
        )
    )
    reducer.handle(
        StepBegin(
            step=par,
            kind="par",
            given=MapStmt(span=SPAN, runnable="child_flow", lanes=1),
        )
    )
    reducer.handle(
        RunBegin(
            run="run_flow_0",
            parent=par,
            control=ControlRef("run_flow_0", 0),
            runnable="flow:child_flow",
            occurrence=Occurrence(
                item=OccurrencePosition(index=0, count=2),
                lane=OccurrencePosition(index=0, count=1),
            ),
        )
    )
    reducer.handle(
        StepBegin(
            step=StepPath.parse("run_flow_0.0"),
            kind="run",
            given=RunStmt(span=SPAN, runnable="fetch"),
        )
    )
    reducer.handle(
        RunBegin(
            run="run_fetch",
            parent=StepPath.parse("run_flow_0.0"),
            control=ControlRef("run_fetch", 0),
            runnable="agic:fetch",
        )
    )
    live = reducer.handle(
        StepBegin(
            step=StepPath.parse("run_fetch.0"),
            kind="tool",
            given=_tool("fetch_page"),
        )
    )
    assert _rows(live.live) == [
        [
            "• running · 0/2 succeeded · 1 active",
            "  0 | #0 | • executing fetch_page",
        ]
    ]
    reducer.handle(
        StepEnd(
            step=StepPath.parse("run_fetch.0"),
            kind="tool",
            status="succeeded",
            output=Local.typed(
                "Part[]",
                (
                    ToolResultPart(
                        tool_call_id="call_1",
                        tool_name="fetch_page",
                        tool_family="web_search",
                        output={"results": [{}, {}]},
                    ),
                ),
                "_",
                0,
            ),
        )
    )
    reducer.handle(RunEnd(run="run_fetch", status="succeeded"))
    flow_finished = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_flow_0.0"),
            kind="run",
            status="succeeded",
            output=_parts("done"),
        )
    )
    flow_live = "\n".join(_rows(flow_finished.live)[0])
    assert "executed fetch_page" in flow_live
    assert "executed tool" not in flow_live
    reducer.handle(RunEnd(run="run_flow_0", status="succeeded"))
    reused = reducer.handle(
        RunBegin(
            run="run_flow_1",
            parent=par,
            control=ControlRef("run_flow_1", 0),
            runnable="flow:child_flow",
            occurrence=Occurrence(
                item=OccurrencePosition(index=1, count=2),
                lane=OccurrencePosition(index=0, count=1),
            ),
        )
    )

    assert _rows(reused.live) == [
        [
            "• running · 1/2 succeeded · 1 active",
            "  0 | #1 | • starting",
        ]
    ]
    reducer.handle(RunEnd(run="run_flow_1", status="succeeded"))
    terminal = reducer.handle(
        StepEnd(
            step=par,
            kind="par",
            status="succeeded",
            output=Local.typed("Part[]", (TextPart("done"),), "_", 1),
        )
    )

    assert _rows(terminal.committed) == [
        [
            "• Mapped all 2 items in parallel",
            "  3 runs · 1 tool call",
            "",
        ]
    ]


def test_finalized_blocks_are_returned_in_step_completion_order() -> None:
    reducer = ProgressProjector(show_boundaries=False)
    reducer.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    for index in range(2):
        reducer.handle(
            StepBegin(
                step=StepPath.parse(f"run_root.{index}"),
                kind="model",
                given=_model(),
            )
        )

    second = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_root.1"),
            kind="model",
            status="succeeded",
            output=_parts("second"),
        )
    )
    first = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_root.0"),
            kind="model",
            status="succeeded",
            output=_parts("first"),
        )
    )

    assert second.committed[0].key == "step:run_root.1"
    assert first.committed[0].key == "step:run_root.0"


def test_model_terminal_preserves_complete_multiline_output() -> None:
    projector = ProgressProjector(show_boundaries=False)
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:demo",
        )
    )
    projector.handle(
        StepBegin(
            step=StepPath.parse("run_root.0"),
            kind="model",
            given=_model(),
        )
    )
    long_line = "x" * 240

    terminal = projector.handle(
        StepEnd(
            step=StepPath.parse("run_root.0"),
            kind="model",
            status="succeeded",
            output=_parts(f"first line\n{long_line}"),
        )
    )

    assert _rows(terminal.committed) == [["• first line", f"  {long_line}"]]


@pytest.mark.parametrize(
    ("status", "termination", "expected"),
    [
        ("succeeded", "exhausted", "• Completed all 3 iterations"),
        ("succeeded", "satisfied", "• Condition met after 2 of 3 iterations"),
        ("failed", "failed", "• Interrupted after completing 1 of 3 iterations"),
        ("canceled", "canceled", "• Canceled before completing an iteration"),
    ],
)
def test_loop_terminal_uses_typed_termination(
    status: StepStatus,
    termination: LoopTermination,
    expected: str,
) -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:demo",
        )
    )
    projector.handle(
        StepBegin(
            step=path,
            kind="loop",
            given=RepeatStmt(span=SPAN, count=3),
        )
    )
    iterations = {"exhausted": 3, "satisfied": 2, "failed": 1, "canceled": 0}[
        termination
    ]

    terminal = projector.handle(
        StepEnd(
            step=path,
            kind="loop",
            status=status,
            noted=LoopStepNoted(
                iterations=iterations,
                termination=termination,
                total=3,
            ),
            error=(
                Pointer.step(StepPath.parse("run_child.0"))
                if status == "failed"
                else None
            ),
        )
    )

    assert _rows(terminal.committed) == [[expected, ""]]
    expected_tone = {
        "succeeded": "normal",
        "failed": "error",
        "canceled": "warning",
    }[status]
    assert terminal.committed[0].rows[0].tone == expected_tone


def test_loop_direct_failure_keeps_error_and_termination_summary() -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:demo",
        )
    )
    projector.handle(
        StepBegin(
            step=path,
            kind="loop",
            given=RepeatStmt(span=SPAN, count=3),
        )
    )

    terminal = projector.handle(
        StepEnd(
            step=path,
            kind="loop",
            status="failed",
            noted=LoopStepNoted(iterations=1, termination="failed"),
            error="condition returned an invalid value",
        )
    )

    assert _rows(terminal.committed) == [
        [
            "• condition returned an invalid value",
            "  Interrupted after completing 1 of 3 iterations",
            "",
        ]
    ]


def test_settle_uses_the_shared_loop_iteration_boundary() -> None:
    projector = ProgressProjector()
    loop = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:demo",
        )
    )
    header = projector.handle(
        StepBegin(
            step=loop,
            kind="loop",
            given=SettleStmt(span=SPAN, runnable="merge_pair"),
        )
    )
    assert _rows(header.committed) == [["[0] Reduce the items with merge_pair", ""]]
    projector.handle(
        RunBegin(
            run="run_merge",
            parent=loop,
            control=ControlRef("run_merge", 0),
            runnable="agic:merge_pair",
            occurrence=Occurrence(
                item=OccurrencePosition(index=0, count=2),
                iteration=IterationOccurrence(index=0, count=2, phase="body"),
            ),
        )
    )

    live = projector.handle(
        StepBegin(
            step=StepPath.parse("run_merge.0"),
            kind="model",
            given=_model(),
        )
    )

    assert _rows(live.committed) == [["--- iteration 1 of 2 ---", ""]]
    assert _rows(live.live) == [["• Thinking..."]]
    assert live.committed[0].gap_before is False
    assert live.live[0].gap_before is False


def test_parallel_terminal_retains_each_independent_failed_lane() -> None:
    projector = ProgressProjector(show_boundaries=False)
    par = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:demo",
        )
    )
    projector.handle(
        StepBegin(
            step=par,
            kind="par",
            given=MapStmt(span=SPAN, runnable="fetch", lanes=2),
        )
    )
    for lane in range(2):
        run_id = f"run_lane_{lane}"
        step = StepPath.parse(f"{run_id}.0")
        projector.handle(
            RunBegin(
                run=run_id,
                parent=par,
                control=ControlRef(run_id, 0),
                runnable="agic:fetch",
                occurrence=Occurrence(
                    item=OccurrencePosition(index=lane, count=2),
                    lane=OccurrencePosition(index=lane, count=2),
                ),
            )
        )
        projector.handle(StepBegin(step=step, kind="model", given=_model()))
        projector.handle(
            StepEnd(
                step=step,
                kind="model",
                status="failed",
                error=f"failure {lane}",
            )
        )
        projector.handle(
            RunEnd(
                run=run_id,
                status="failed",
                error=Pointer.step(step),
            )
        )

    terminal = projector.handle(
        StepEnd(
            step=par,
            kind="par",
            status="failed",
            error="parallel step stopped because lane 0 (#0) failed",
        )
    )
    text = "\n".join(_rows(terminal.committed)[0])

    assert "0 | #0 | • failed failure 0" in text
    assert "1 | #1 | • failed failure 1" in text
    assert text.count("parallel step stopped") == 1


def test_nested_parallel_direct_error_is_preserved_by_the_outer_lane() -> None:
    projector = ProgressProjector(show_boundaries=False)
    outer = StepPath.parse("run_root.0")
    inner = StepPath.parse("run_child.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:demo",
        )
    )
    projector.handle(
        StepBegin(
            step=outer,
            kind="par",
            given=MapStmt(span=SPAN, runnable="child", lanes=1),
        )
    )
    projector.handle(
        RunBegin(
            run="run_child",
            parent=outer,
            control=ControlRef("run_child", 0),
            runnable="flow:child",
            occurrence=Occurrence(
                item=OccurrencePosition(index=0, count=1),
                lane=OccurrencePosition(index=0, count=1),
            ),
        )
    )
    projector.handle(
        StepBegin(
            step=inner,
            kind="par",
            given=MapStmt(span=SPAN, runnable="leaf", lanes=1),
        )
    )
    projector.handle(
        StepEnd(
            step=inner,
            kind="par",
            status="failed",
            error="input must be a list",
        )
    )
    projector.handle(
        RunEnd(
            run="run_child",
            status="failed",
            error=Pointer.step(inner),
        )
    )

    terminal = projector.handle(
        StepEnd(
            step=outer,
            kind="par",
            status="failed",
            error="parallel step stopped because lane 0 (#0) failed",
        )
    )

    assert _rows(terminal.committed) == [
        [
            "• Parallel execution stopped: 0/1 succeeded and 1 failed",
            "  0 | #0 | • input must be a list",
            "",
            "• parallel step stopped because lane 0 (#0) failed",
            "  1 run",
            "",
        ]
    ]


def test_nested_parallel_boundary_error_does_not_replace_the_leaf_error() -> None:
    projector = ProgressProjector(show_boundaries=False)
    outer = StepPath.parse("run_root.0")
    inner = StepPath.parse("run_child.0")
    leaf = StepPath.parse("run_leaf.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:demo",
        )
    )
    projector.handle(
        StepBegin(
            step=outer,
            kind="par",
            given=MapStmt(span=SPAN, runnable="child", lanes=1),
        )
    )
    projector.handle(
        RunBegin(
            run="run_child",
            parent=outer,
            control=ControlRef("run_child", 0),
            runnable="flow:child",
            occurrence=Occurrence(
                item=OccurrencePosition(index=0, count=1),
                lane=OccurrencePosition(index=0, count=1),
            ),
        )
    )
    projector.handle(
        StepBegin(
            step=inner,
            kind="par",
            given=MapStmt(span=SPAN, runnable="leaf", lanes=1),
        )
    )
    projector.handle(
        RunBegin(
            run="run_leaf",
            parent=inner,
            control=ControlRef("run_leaf", 0),
            runnable="agic:leaf",
            occurrence=Occurrence(
                item=OccurrencePosition(index=0, count=1),
                lane=OccurrencePosition(index=0, count=1),
            ),
        )
    )
    projector.handle(StepBegin(step=leaf, kind="model", given=_model()))
    projector.handle(
        StepEnd(
            step=leaf,
            kind="model",
            status="failed",
            error="provider returned status 429",
        )
    )
    projector.handle(
        RunEnd(
            run="run_leaf",
            status="failed",
            error=Pointer.step(leaf),
        )
    )
    projector.handle(
        StepEnd(
            step=inner,
            kind="par",
            status="failed",
            error="parallel step stopped because lane 0 (#0) failed",
        )
    )
    projector.handle(
        RunEnd(
            run="run_child",
            status="failed",
            error=Pointer.step(inner),
        )
    )

    terminal = projector.handle(
        StepEnd(
            step=outer,
            kind="par",
            status="failed",
            error="parallel step stopped because lane 0 (#0) failed",
        )
    )
    text = "\n".join(_rows(terminal.committed)[0])

    assert "0 | #0 | • failed provider returned status 429" in text
    assert text.count("parallel step stopped") == 1
