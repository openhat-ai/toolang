from __future__ import annotations

import pytest

from toolang.base.types.message import TextDelta, TextPart, ToolResultPart
from toolang.base.types.run import ModelCall, ToolCall
from toolang.cli.common.execution_progress import (
    ExecutionProgressReducer,
    ProgressBlock,
)
from toolang.cli.common.execution_progress.formatting import progress_statement_header
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
    ModelStepGiven,
    ModelStepNoted,
    ModelTokenCount,
    Occurrence,
    OccurrencePosition,
    Pointer,
    StepPath,
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
        progress_statement_header(
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
        progress_statement_header(MapStmt(span=SPAN, runnable="search_web", lanes=4))
        == "Run search_web for each item, up to 4 at once"
    )
    assert (
        progress_statement_header(
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
        progress_statement_header(LetStmt(span=SPAN, binding="topic", value="x"))
        == "Set topic"
    )
    assert (
        progress_statement_header(
            RunStmt(span=SPAN, runnable="<agic:12>", binding=None)
        )
        == "Run the inline task without saving the result"
    )
    assert (
        progress_statement_header(KeepStmt(span=SPAN, position="first", count=1))
        == "Keep the first item"
    )
    assert (
        progress_statement_header(
            StormStmt(span=SPAN, count=3, runnable="review_item", lanes=2)
        )
        == "Run review_item 3 times, up to 2 at once"
    )
    assert (
        progress_statement_header(RepeatStmt(span=SPAN, count=3, runnable="<agic:20>"))
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
    assert progress_statement_header(statement) == expected


def test_model_live_row_becomes_one_stable_output_with_annotations() -> None:
    reducer = ExecutionProgressReducer()
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
    assert _rows(final.stable) == [
        [
            "· executed Use a shared reducer.",
            "  run_root.0 · 1.8s · deepseek/deepseek-chat · 3.4k/86 tokens · $0.002",
        ]
    ]


def test_flow_run_header_wraps_real_agic_steps_without_a_wrapper_row() -> None:
    reducer = ExecutionProgressReducer()
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
    stable = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_child.0"),
            kind="model",
            status="succeeded",
            output=_parts("Done."),
        )
    )
    assert _rows(stable.stable) == [
        [
            "[0] Run summarize",
            "",
            "· executed Done.",
            "  run_child.0 · deepseek/deepseek-chat",
        ]
    ]
    reducer.handle(RunEnd(run="run_child", status="succeeded"))
    wrapper = reducer.handle(
        StepEnd(
            step=StepPath.parse("run_root.0"),
            kind="run",
            status="succeeded",
            output=_parts("Done."),
        )
    )
    assert wrapper.stable == ()


def test_tool_output_uses_one_unmarked_output_row() -> None:
    reducer = ExecutionProgressReducer(show_boundaries=False)
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
    assert _rows(update.stable) == [
        ["· executed web_search.search", "  5 results", "  run_root.0"]
    ]


def test_flow_scalar_output_is_quoted_in_its_normal_output_slot() -> None:
    reducer = ExecutionProgressReducer(show_boundaries=False)
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

    assert _rows(update.stable) == [['· executed "agent runtimes"', "  run_root.0"]]


def test_repeat_uses_flat_iteration_and_statement_boundaries() -> None:
    reducer = ExecutionProgressReducer()
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
        )
    )

    assert _rows(completed.stable) == [
        ["· completed · 1 iteration", "  1 run succeeded · 1 model call"]
    ]


def test_until_run_shows_control_boundary_and_only_real_agic_steps() -> None:
    reducer = ExecutionProgressReducer()
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
    text = "\n".join(_rows(final.stable)[0])
    assert "· executed true" in text
    assert "executed completion_check" not in text


def test_parallel_lane_is_single_line_and_terminal_failure_replaces_lanes() -> None:
    reducer = ExecutionProgressReducer(show_boundaries=False)
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
            "· running · 1 failed · 1 canceling",
            "  0 | #4 | · failed fetch_page · provider returned status 429",
            "  1 | #5 | · canceling…",
        ]
    ]
    reducer.handle(
        RunEnd(
            run="run_child_0",
            status="failed",
            error=Pointer.step(StepPath.parse("run_child_0.0")),
        )
    )
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
    assert _rows(terminal.stable) == [
        [
            "· 1 failed · 1 canceled",
            "  parallel step stopped because lane 0 (#4) failed",
            "  run_root.0 · 2 runs · 1 model call · 1 tool call",
        ]
    ]


def test_parent_error_pointers_are_silent_but_ownerless_run_errors_are_visible() -> (
    None
):
    reducer = ExecutionProgressReducer(show_boundaries=False)
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
    assert "connection closed" in "\n".join(_rows(leaf.stable)[0])
    root = reducer.handle(
        RunEnd(
            run="run_root",
            status="failed",
            error=Pointer.step(StepPath.parse("run_root.0")),
        )
    )
    assert root.stable == ()

    ownerless = ExecutionProgressReducer()
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
    assert _rows(update.stable) == [["· progress stream ended before run completion"]]


def test_malformed_part_sequence_becomes_one_root_diagnostic() -> None:
    reducer = ExecutionProgressReducer()
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
    assert "PartDelta without active Part" in _rows(update.stable)[0][0]


def test_run_end_with_active_step_clears_live_with_one_diagnostic() -> None:
    reducer = ExecutionProgressReducer()
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
    assert _rows(update.stable) == [["· RunEnd with active Step for run_root"]]


def test_cyclic_error_pointer_becomes_one_terminal_diagnostic() -> None:
    reducer = ExecutionProgressReducer(show_boundaries=False)
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

    assert _rows(update.stable) == [["· could not resolve execution error run_root.0"]]


def test_compact_mode_removes_repeat_boundaries_but_keeps_agic_activity() -> None:
    reducer = ExecutionProgressReducer(show_boundaries=False)
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
    reducer = ExecutionProgressReducer(show_boundaries=False)
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
    reducer.handle(
        StepEnd(
            step=StepPath.parse("run_flow_0.0"),
            kind="run",
            status="succeeded",
            output=_parts("done"),
        )
    )
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


def test_stable_blocks_are_returned_in_step_completion_order() -> None:
    reducer = ExecutionProgressReducer(show_boundaries=False)
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

    assert second.stable[0].key == "step:run_root.1"
    assert first.stable[0].key == "step:run_root.0"
