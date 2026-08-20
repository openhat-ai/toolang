from __future__ import annotations

import pytest

from toolang.base.types.message import (
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


def _tool(name: str = "web_search.search") -> ToolStepGiven:
    return ToolStepGiven(
        plugin=name.split(".", 1)[0],
        call=ToolCall(
            tool_call_id="call_1",
            call_id="call_1",
            name=name,
            input={},
        ),
    )


def _parts(text: str) -> Local:
    return Local.typed("Part[]", (TextPart(text),), "_", 0)


def _rows(blocks: tuple[ProgressBlock, ...]) -> list[list[str]]:
    return [[row.text for row in block.rows] for block in blocks]


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


def test_model_live_row_becomes_one_complete_finalized_output() -> None:
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
    assert _rows(live.live) == [["· thinking…"]]

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
            delta=TextDelta(text="Comparing approaches"),
        )
    )
    assert _rows(streamed.live) == [["· thinking… Comparing approaches"]]
    reducer.handle(
        PartEnd(
            step=StepPath.parse("run_root.0"),
            part=0,
            data=TextPart("Comparing approaches"),
        )
    )

    final = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_root.0"),
            kind="model",
            status="succeeded",
            output=_parts("Use a shared reducer."),
            noted=ModelStepNoted(
                tokens=ModelTokenCount(input=3400, output=86),
                cost="0.002",
            ),
            finished_at="2026-01-01T00:00:01.800Z",
        )
    )
    assert _rows(final.live) == []
    assert _rows(final.finalized) == [["· Use a shared reducer."]]


def test_model_tool_request_uses_a_typed_summary_instead_of_runtime_repr() -> None:
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
            output=Local.typed(
                "Part[]",
                (
                    ToolCallPart(
                        tool_call_id="call_1",
                        tool_name="web_search.search",
                        tool_family="web_search",
                        input={"query": "agent runtimes"},
                    ),
                ),
                "_",
                0,
            ),
        )
    )

    assert _rows(update.finalized) == [["· requested web_search.search"]]
    assert "Array(" not in update.finalized[0].rows[0].text
    assert reducer._steps == {}


def test_flow_run_header_wraps_real_agic_steps_without_a_wrapper_row() -> None:
    reducer = ProgressProjector()
    reducer.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:research",
        )
    )
    reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.0"),
            kind="run",
            given=RunStmt(span=SPAN, runnable="summarize"),
            started_at="2026-01-01T00:00:00Z",
        )
    )
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
    assert _rows(live.live) == [["[0] Run summarize", "", "· thinking…"]]
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
    assert _rows(finalized.finalized) == [
        [
            "[0] Run summarize",
            "",
            "· Done.",
        ]
    ]
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
    assert _rows(wrapper.finalized) == [
        [
            "  2.0s · 1 run · 1 model call · ↑639 ↓215 $0.00",
            "",
        ]
    ]


def test_tool_output_preserves_complete_unmarked_output_rows() -> None:
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
            given=_tool(),
        )
    )
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
                        output={"results": [{}, {}, {}, {}, {}]},
                    ),
                ),
                "_",
                0,
            ),
        )
    )
    assert _rows(update.finalized) == [
        [
            "· executed web_search.search",
            "  {",
            '    "results": [',
            "      {},",
            "      {},",
            "      {},",
            "      {},",
            "      {}",
            "    ]",
            "  }",
        ]
    ]


def test_flow_scalar_output_is_quoted_in_its_normal_output_slot() -> None:
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

    assert _rows(update.finalized) == [["· agent runtimes", ""]]


def test_repeat_uses_flat_iteration_and_statement_boundaries() -> None:
    reducer = ProgressProjector()
    reducer.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:work",
        )
    )
    reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.2"),
            kind="loop",
            given=RepeatStmt(span=SPAN, count=3, runnable="completion_check"),
        )
    )
    reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.2.0"),
            kind="run",
            given=RunStmt(span=SPAN, runnable="review"),
            occurrence=Occurrence(
                iteration=IterationOccurrence(index=0, count=3, phase="body")
            ),
        )
    )
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
    assert _rows(live.live) == [
        [
            "[2] Repeat up to 3 times",
            "",
            "--- iteration 1 of 3 ---",
            "",
            "[0] Run review",
            "",
            "· thinking…",
        ]
    ]
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

    assert _rows(completed.finalized) == [
        ["· completed 1 iteration", "  1 run · 1 model call", ""]
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
    reducer.handle(
        StepBegin(
            step=StepPath.parse("run_root.0"),
            kind="loop",
            given=RepeatStmt(span=SPAN, count=3, runnable="completion_check"),
        )
    )
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
    assert _rows(live.live) == [
        [
            "[0] Repeat up to 3 times",
            "",
            "<?> completion_check",
            "",
            "· thinking…",
        ]
    ]
    final = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_until.0"),
            kind="model",
            status="succeeded",
            output=_parts("true"),
        )
    )
    text = "\n".join(_rows(final.finalized)[0])
    assert "· true" in text
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
            "· running · 2 active",
            "  0 | #4 | · failed fetch_page · provider returned status 429",
            "  1 | #5 | · thinking…",
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
            "· running · 1 failed · 1 canceling",
            "  0 | #4 | · failed fetch_page · provider returned status 429",
            "  1 | #5 | · canceling…",
        ]
    ]
    reducer.handle(
        StepEnd(
            step=StepPath.parse("run_child_1.0"),
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
    assert _rows(terminal.finalized) == [
        [
            "· 1 failed · 1 canceled",
            "  0 | #4 | · failed fetch_page",
            "             provider returned status 429",
            "",
            "· parallel step stopped because lane 0 (#4) failed",
            "  2 runs · 1 model call · 1 tool call",
            "",
        ]
    ]


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
    assert "connection closed" in "\n".join(_rows(leaf.finalized)[0])
    root = reducer.handle(
        RunEnd(
            run="run_root",
            status="failed",
            error=Pointer.step(StepPath.parse("run_root.0")),
        )
    )
    assert root.finalized == ()

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
    assert _rows(update.finalized) == [
        ["· progress stream ended before run completion"]
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
    assert "PartDelta without active Part" in _rows(update.finalized)[0][0]


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
    assert _rows(update.finalized) == [["· RunEnd with active Step for run_root"]]


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

    assert _rows(update.finalized) == [
        ["· could not resolve execution error run_root.0"]
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

    assert _rows(live.live) == [["· thinking…"]]


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
            "· running · 1 active",
            "  0 | #0 | · executing fetch_page…",
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
            "· running · 1 succeeded · 1 active",
            "  0 | #1 | · starting…",
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

    assert _rows(terminal.finalized) == [
        [
            "· 2 succeeded",
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

    assert second.finalized[0].key == "step:run_root.1"
    assert first.finalized[0].key == "step:run_root.0"


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

    assert _rows(terminal.finalized) == [["· first line", f"  {long_line}"]]


@pytest.mark.parametrize(
    ("status", "termination", "expected"),
    [
        ("succeeded", "exhausted", "· completed 3 iterations"),
        ("succeeded", "satisfied", "· condition met after 2 iterations"),
        ("failed", "failed", "· interrupted after 1 iteration"),
        ("canceled", "canceled", "· canceled after 0 iterations"),
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
            ),
            error=(
                Pointer.step(StepPath.parse("run_child.0"))
                if status == "failed"
                else None
            ),
        )
    )

    assert _rows(terminal.finalized) == [[expected, ""]]


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

    assert _rows(terminal.finalized) == [
        [
            "· condition returned an invalid value",
            "  interrupted after 1 iteration",
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
    projector.handle(
        StepBegin(
            step=loop,
            kind="loop",
            given=SettleStmt(span=SPAN, runnable="merge_pair"),
        )
    )
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

    assert _rows(live.live) == [
        [
            "[0] Reduce the items with merge_pair",
            "",
            "--- iteration 1 of 2 ---",
            "",
            "· thinking…",
        ]
    ]


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
    text = "\n".join(_rows(terminal.finalized)[0])

    assert "0 | #0 | · failed failure 0" in text
    assert "1 | #1 | · failed failure 1" in text
    assert text.count("parallel step stopped") == 1
