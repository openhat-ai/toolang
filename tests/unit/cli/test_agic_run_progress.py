"""Dynamic Agic Run Step progress grammar."""

from __future__ import annotations

from io import StringIO
import re

import pytest

from toolang.base.types.message import TextPart, ToolCallPart, ToolResultPart
from toolang.base.types.run import ModelCall
from toolang.cli.common.execution_progress import (
    ProgressBlock,
    ProgressProjector,
    ProgressRow,
    ProgressUpdate,
)
from toolang.cli.common.execution_progress.formatting import display_width
from toolang.cli.common.execution_progress.rich_rendering import run_footer_renderable
from toolang.cli.common.script_progress.console import ProgressConsole
from toolang.cli.toolang.commands.chat import blocks, rendering
from toolang.execution.events import RunBegin, RunEnd, StepBegin, StepEnd
from toolang.execution.types import (
    ControlRef,
    Local,
    ModelStepGiven,
    ModelStepNoted,
    ModelTokenCount,
    Occurrence,
    OccurrencePosition,
    StepPath,
)
from toolang.lang.ast import MapStmt, RunStmt, Span


class _TtyStream(StringIO):
    def isatty(self) -> bool:
        return True


def _run_stmt(runnable: str = "agic:summarize") -> RunStmt:
    return RunStmt(span=Span(line=1), runnable=runnable)


def _model_given() -> ModelStepGiven:
    return ModelStepGiven(
        model="test/scripted",
        call=ModelCall(instructions="", messages=[]),
    )


def _render_progress(block: ProgressBlock, *, width: int = 80) -> str:
    stream = StringIO()
    ProgressConsole(stream, width=width, max_width=width).apply(
        ProgressUpdate(committed=(block,))
    )
    return stream.getvalue()


def _render_chat_progress(block: ProgressBlock, *, width: int = 80) -> str:
    renderable = blocks.ExecutionProgressBlock(
        block,
        max_width=width,
    ).render()
    return "".join(
        segment.text
        for segment in rendering.render_segments(renderable, width=width)
        if not segment.control
    )


def test_dynamic_run_projects_a_flat_header_and_child_id_footer() -> None:
    projector = ProgressProjector(show_boundaries=False)
    dynamic = StepPath.parse("run_root.0")
    child_model = StepPath.parse("run_child.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:parent",
            started_at="2026-01-01T00:00:00Z",
        )
    )

    starting = projector.handle(
        StepBegin(
            step=dynamic,
            kind="run",
            given=_run_stmt(),
            started_at="2026-01-01T00:00:00Z",
        )
    )
    assert starting.committed == ()
    assert starting.live[0].rows == (
        ProgressRow("• Running agic:summarize...", "active"),
    )

    header = projector.handle(
        RunBegin(
            run="run_child",
            control=ControlRef("run_child", 0),
            runnable="agic:summarize",
            parent=dynamic,
            started_at="2026-01-01T00:00:00.100Z",
        )
    )
    assert len(header.committed) == 1
    assert header.live == ()
    assert header.committed[0].rows == (
        ProgressRow("---  run agic:summarize", leader="hyphen"),
        ProgressRow(""),
    )
    projector.handle(
        StepBegin(
            step=child_model,
            kind="model",
            given=ModelStepGiven(
                model="test/scripted",
                call=ModelCall(instructions="", messages=[]),
            ),
            started_at="2026-01-01T00:00:00.200Z",
        )
    )
    projector.handle(
        StepEnd(
            step=child_model,
            kind="model",
            status="succeeded",
            output=Local.typed("Part[]", (TextPart("summary"),), "_", 0),
            noted=ModelStepNoted(tokens=ModelTokenCount(input=4, output=2)),
            finished_at="2026-01-01T00:00:01Z",
        )
    )
    projector.handle(
        RunEnd(
            run="run_child",
            status="succeeded",
            finished_at="2026-01-01T00:00:01.100Z",
        )
    )
    footer = projector.handle(
        StepEnd(
            step=dynamic,
            kind="run",
            status="succeeded",
            finished_at="2026-01-01T00:00:02Z",
        )
    )

    assert len(footer.committed) == 1
    row = footer.committed[0].rows[0]
    assert row.text == "---  "
    assert row.leader == "hyphen"
    assert row.facts == ("2.0s", "1 run", "1 model call", "↑4 ↓2")
    assert row.right_status == "succeeded"
    assert row.right_identity == "run_child"
    assert footer.committed[0].rows[-1] == ProgressRow("")


def test_dynamic_run_preaccept_failure_uses_a_trace_marker_without_boundaries() -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:parent",
        )
    )
    starting = projector.handle(
        StepBegin(
            step=path,
            kind="run",
            given=_run_stmt("flow:missing"),
        )
    )
    assert starting.committed == ()
    assert starting.live[0].rows == (
        ProgressRow("• Running flow:missing...", "active"),
    )

    terminal = projector.handle(
        StepEnd(
            step=path,
            kind="run",
            status="failed",
            error="Runnable not found: missing",
        )
    )

    assert terminal.live == ()
    assert terminal.committed[0].rows == (
        ProgressRow("• Failed to run flow:missing", "error"),
        ProgressRow("  Runnable not found: missing", "error"),
    )
    rendered = _render_progress(terminal.committed[0], width=72)
    assert rendered.splitlines() == [
        "",
        "• Failed to run flow:missing",
        "  Runnable not found: missing",
    ]


def test_dynamic_run_dividers_align_and_preserve_complete_identity_when_narrow() -> (
    None
):
    wide = StringIO()
    ProgressConsole(wide, width=72).apply(
        ProgressUpdate(
            committed=(
                ProgressBlock(
                    "dynamic",
                    (
                        ProgressRow(
                            "---  run agic:summarize",
                            leader="hyphen",
                        ),
                        ProgressRow(
                            "---  ",
                            leader="hyphen",
                            facts=("2.0s", "1 run", "1 model call"),
                            right_status="succeeded",
                            right_identity="run_abc123",
                        ),
                    ),
                ),
            )
        )
    )
    wide_lines = wide.getvalue().splitlines()
    assert wide_lines[0].startswith("╓ run agic:summarize ───")
    assert wide_lines[1].startswith("╙ 2.0s · 1 run · 1 model call ───")
    assert wide_lines[1].endswith("succeeded run_abc123")
    assert all(display_width(line) == 72 for line in wide_lines)

    narrow = StringIO()
    ProgressConsole(narrow, width=32).apply(
        ProgressUpdate(
            committed=(
                ProgressBlock(
                    "dynamic",
                    (
                        ProgressRow(
                            "---  ",
                            leader="hyphen",
                            facts=(
                                "31.0s",
                                "6 runs",
                                "12 model calls",
                                "8 tool calls",
                            ),
                            right_status="succeeded",
                            right_identity="run_complete_identity",
                        ),
                    ),
                ),
            )
        )
    )
    narrow_lines = narrow.getvalue().splitlines()
    assert "run_complete_identity" in "\n".join(narrow_lines)
    assert all(display_width(line) <= 32 for line in narrow_lines)

    tiny = StringIO()
    ProgressConsole(tiny, width=5, max_width=5).apply(
        ProgressUpdate(
            committed=(
                ProgressBlock(
                    "dynamic",
                    (
                        ProgressRow(
                            "---  run agic:总结",
                            leader="hyphen",
                        ),
                        ProgressRow(
                            "---  ",
                            leader="hyphen",
                            facts=("2.0s", "1 run"),
                            right_status="succeeded",
                            right_identity="run_完整",
                        ),
                    ),
                ),
            )
        )
    )
    tiny_lines = tiny.getvalue().splitlines()
    compact = "".join(line.replace(" ", "") for line in tiny_lines)
    assert "runagic:总结" in compact
    assert "2.0s1run" in compact
    assert "succeededrun_完整" in compact
    assert all(display_width(line) <= 5 for line in tiny_lines)


def test_flow_owned_run_step_keeps_numbered_header_and_step_path_footer() -> None:
    projector = ProgressProjector()
    step = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:publish",
        )
    )

    header = projector.handle(
        StepBegin(step=step, kind="run", given=_run_stmt("summarize"))
    )
    assert header.committed[0].rows == (
        ProgressRow("[0] Run summarize"),
        ProgressRow(""),
    )

    projector.handle(
        RunBegin(
            run="run_child",
            control=ControlRef("run_child", 0),
            runnable="agic:summarize",
            parent=step,
        )
    )
    projector.handle(RunEnd(run="run_child", status="succeeded"))
    terminal = projector.handle(StepEnd(step=step, kind="run", status="succeeded"))

    footer = terminal.committed[0].rows[-2]
    assert footer.right_text == "run_root.0"
    assert footer.right_status == ""
    assert footer.right_identity == ""
    assert all(row.leader == "none" for row in terminal.committed[0].rows)


def test_agic_to_flow_keeps_child_flow_run_steps_numbered() -> None:
    projector = ProgressProjector()
    dynamic = StepPath.parse("run_root.0")
    authored = StepPath.parse("run_publish.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:parent",
        )
    )
    projector.handle(
        StepBegin(
            step=dynamic,
            kind="run",
            given=_run_stmt("flow:publish"),
        )
    )
    outer_header = projector.handle(
        RunBegin(
            run="run_publish",
            control=ControlRef("run_publish", 0),
            runnable="flow:publish",
            parent=dynamic,
        )
    )

    flow_header = projector.handle(
        StepBegin(
            step=authored,
            kind="run",
            given=_run_stmt("validate"),
        )
    )
    projector.handle(
        RunBegin(
            run="run_validate",
            control=ControlRef("run_validate", 0),
            runnable="agic:validate",
            parent=authored,
        )
    )
    projector.handle(RunEnd(run="run_validate", status="succeeded"))
    flow_footer = projector.handle(
        StepEnd(step=authored, kind="run", status="succeeded")
    )
    projector.handle(RunEnd(run="run_publish", status="succeeded"))
    outer_footer = projector.handle(
        StepEnd(step=dynamic, kind="run", status="succeeded")
    )

    assert outer_header.committed[0].rows[0] == ProgressRow(
        "---  run flow:publish",
        leader="hyphen",
    )
    assert flow_header.committed[0].rows[0].text == "[0] Run validate"
    assert flow_footer.committed[0].rows[-2].right_text == "run_publish.0"
    assert outer_footer.committed[0].rows[0].right_identity == "run_publish"


def test_nested_dynamic_run_footers_pair_with_their_direct_children() -> None:
    projector = ProgressProjector(show_boundaries=False)
    outer = StepPath.parse("run_root.0")
    inner = StepPath.parse("run_child.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:parent",
        )
    )
    projector.handle(StepBegin(step=outer, kind="run", given=_run_stmt("agic:child")))
    outer_header = projector.handle(
        RunBegin(
            run="run_child",
            control=ControlRef("run_child", 0),
            runnable="agic:child",
            parent=outer,
        )
    )
    projector.handle(StepBegin(step=inner, kind="run", given=_run_stmt("agic:leaf")))
    inner_header = projector.handle(
        RunBegin(
            run="run_leaf",
            control=ControlRef("run_leaf", 0),
            runnable="agic:leaf",
            parent=inner,
        )
    )
    projector.handle(RunEnd(run="run_leaf", status="succeeded"))
    inner_footer = projector.handle(StepEnd(step=inner, kind="run", status="succeeded"))
    projector.handle(RunEnd(run="run_child", status="succeeded"))
    outer_footer = projector.handle(StepEnd(step=outer, kind="run", status="succeeded"))

    assert outer_header.committed[0].rows[0].text == "---  run agic:child"
    assert inner_header.committed[0].rows[0].text == "---  run agic:leaf"
    assert inner_footer.committed[0].rows[0].right_identity == "run_leaf"
    assert outer_footer.committed[0].rows[0].right_identity == "run_child"
    assert all(
        row.text.startswith("---  ")
        for row in (
            outer_header.committed[0].rows[0],
            inner_header.committed[0].rows[0],
            inner_footer.committed[0].rows[0],
            outer_footer.committed[0].rows[0],
        )
    )


def test_dynamic_run_inside_parallel_lane_stays_on_one_lane_row() -> None:
    projector = ProgressProjector()
    parallel = StepPath.parse("run_root.0")
    dynamic = StepPath.parse("run_child.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="flow:batch",
        )
    )
    projector.handle(
        StepBegin(
            step=parallel,
            kind="par",
            given=MapStmt(span=Span(line=1), runnable="worker", lanes=1),
        )
    )
    projector.handle(
        RunBegin(
            run="run_child",
            control=ControlRef("run_child", 0),
            runnable="agic:worker",
            parent=parallel,
            occurrence=Occurrence(
                item=OccurrencePosition(index=0, count=1),
                lane=OccurrencePosition(index=0, count=1),
            ),
        )
    )

    update = projector.handle(
        StepBegin(step=dynamic, kind="run", given=_run_stmt("agic:leaf"))
    )

    rows = update.live[0].rows
    assert len(rows) == 2
    assert rows[1].text.endswith("• Running agic:leaf...")
    assert "---" not in " ".join(row.text for row in rows)

    projector.handle(
        RunBegin(
            run="run_leaf",
            control=ControlRef("run_leaf", 0),
            runnable="agic:leaf",
            parent=dynamic,
        )
    )
    projector.handle(RunEnd(run="run_leaf", status="succeeded"))
    terminal = projector.handle(StepEnd(step=dynamic, kind="run", status="succeeded"))

    assert terminal.committed == ()
    assert terminal.live[0].rows[1].text.endswith("• Ran agic:leaf")
    assert "---" not in " ".join(row.text for row in terminal.live[0].rows)


@pytest.mark.parametrize(
    ("status", "border_color"),
    [
        ("succeeded", None),
        ("failed", "red"),
        ("canceled", "yellow"),
    ],
)
def test_dynamic_footer_colors_only_the_terminal_border(
    status: str,
    border_color: str | None,
) -> None:
    block = ProgressBlock(
        "dynamic",
        (
            ProgressRow(
                "---  ",
                leader="hyphen",
                facts=("2.0s", "1 run"),
                right_status=status,
                right_identity="run_child",
            ),
        ),
    )
    segments = [
        segment
        for segment in rendering.render_segments(
            blocks.ExecutionProgressBlock(block, max_width=72).render(),
            width=72,
        )
        if segment.text.strip()
    ]

    marker = next(segment for segment in segments if "╙" in segment.text)
    border = next(segment for segment in segments if "─" in segment.text)
    facts = next(segment for segment in segments if "2.0s" in segment.text)
    terminal = next(segment for segment in segments if status in segment.text)
    identity = next(segment for segment in segments if "run_child" in segment.text)
    for segment in (facts, terminal, identity):
        assert segment.style is not None
        assert segment.style.dim
        assert segment.style.color is None
    for segment in (marker, border):
        assert segment.style is not None
        if border_color is None:
            assert segment.style.dim
            assert segment.style.color is None
        else:
            assert segment.style.color is not None
            assert segment.style.color.name == border_color


def test_dynamic_scope_suppresses_internal_call_and_protocol_result_rows() -> None:
    projector = ProgressProjector(show_boundaries=False)
    model = StepPath.parse("run_root.0")
    dynamic = StepPath.parse("run_root.1")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:parent",
        )
    )
    projector.handle(StepBegin(step=model, kind="model", given=_model_given()))
    hidden = projector.handle(
        StepEnd(
            step=model,
            kind="model",
            status="succeeded",
            output=Local.typed(
                "Part[]",
                (
                    ToolCallPart(
                        tool_call_id="runtime-call",
                        tool_name="_internal_run_action",
                        tool_family="runtime",
                        input={"runnable": "agic:child"},
                    ),
                ),
                "_",
                0,
            ),
        )
    )
    projector.handle(StepBegin(step=dynamic, kind="run", given=_run_stmt("agic:child")))
    header = projector.handle(
        RunBegin(
            run="run_child",
            control=ControlRef("run_child", 0),
            runnable="agic:child",
            parent=dynamic,
        )
    )
    projector.handle(RunEnd(run="run_child", status="succeeded"))
    footer = projector.handle(
        StepEnd(
            step=dynamic,
            kind="run",
            status="succeeded",
            output=Local.typed(
                "Part[]",
                (
                    ToolResultPart(
                        tool_call_id="runtime-call",
                        tool_name="_internal_run_action",
                        tool_family="runtime",
                        output={"run": "run_child"},
                    ),
                ),
                "_",
                0,
            ),
        )
    )

    visible = " ".join(
        row.text
        for update in (hidden, header, footer)
        for block in update.committed
        for row in block.rows
    )
    assert hidden.committed == ()
    assert "_internal_run_action" not in visible
    assert "runtime-call" not in visible
    assert "run agic:child" in visible


def test_dynamic_header_normalizes_untrusted_runnable_text() -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepPath.parse("run_root.0")
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef("run_root", 0),
            runnable="agic:parent",
        )
    )

    projector.handle(
        StepBegin(
            step=path,
            kind="run",
            given=_run_stmt("flow:\x00missing\nname" + "x" * 300),
        )
    )
    header = projector.handle(
        RunBegin(
            run="run_child",
            control=ControlRef("run_child", 0),
            runnable="flow:missing",
            parent=path,
        )
    )
    caption = header.committed[0].rows[0].text

    assert caption.startswith("---  run flow: missing name")
    assert "\x00" not in caption
    assert "\n" not in caption
    assert len(caption.removeprefix("---  run ")) == 240


def test_script_and_chat_render_dynamic_boundaries_identically() -> None:
    progress = ProgressBlock(
        "dynamic",
        (
            ProgressRow("---  run agic:summarize", leader="hyphen"),
            ProgressRow(""),
            ProgressRow("• Summary", "normal"),
            ProgressRow(""),
            ProgressRow(
                "---  ",
                leader="hyphen",
                facts=("2.0s", "1 run", "1 model call"),
                right_status="succeeded",
                right_identity="run_child",
            ),
            ProgressRow(""),
        ),
    )

    script = _render_progress(progress, width=72)
    chat = _render_chat_progress(
        progress,
        width=72,
    )
    tty_stream = _TtyStream()
    ProgressConsole(tty_stream, width=72).apply(ProgressUpdate(committed=(progress,)))
    tty = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", tty_stream.getvalue())

    assert script == chat == tty


def test_dynamic_boundaries_keep_single_blank_row_between_sections() -> None:
    progress = ProgressBlock(
        "dynamic",
        (
            ProgressRow("---  run agic:summarize", leader="hyphen"),
            ProgressRow(""),
            ProgressRow("• Summary", "normal"),
            ProgressRow(""),
            ProgressRow(
                "---  ",
                leader="hyphen",
                facts=("2.0s", "1 run", "1 model call"),
                right_status="succeeded",
                right_identity="run_child",
            ),
            ProgressRow(""),
            ProgressRow("• Parent continues", "normal"),
        ),
    )
    rendered = _render_progress(progress, width=72)

    assert "\n\n\n" not in rendered
    assert re.search(r"╓ run agic:summarize ─+\n\n• Summary", rendered)
    assert re.search(r"• Summary\n\n╙ 2.0s", rendered)
    assert re.search(r"succeeded run_child\n\n• Parent continues", rendered)


def test_root_run_footer_grammar_is_unchanged() -> None:
    stream = StringIO()
    console = ProgressConsole(stream, width=72)
    console.write_renderable(
        run_footer_renderable(
            run_id="run_root123",
            status="succeeded",
            facts=("8.2s", "4 runs", "6 model calls", "2 tool calls"),
            max_width=72,
            gap_before=False,
        )
    )

    assert stream.getvalue() == (
        "∎ run_root123 succeeded     8.2s · 4 runs · 6 model calls · 2 tool calls\n"
    )
