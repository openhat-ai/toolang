"""Dynamic Agic Run Step progress grammar."""

from __future__ import annotations


from tests.support.observations import run_begin, run_end, step_begin, step_end


from io import StringIO
import re
from typing import Literal

import pytest

from toolang.base.types.message import TextPart, ToolCallPart, ToolResultPart
from toolang.base.types.run import ModelCall, ToolCall
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
from toolang.execution.types import (
    Output,
    ControlRef,
    ErrorMessage,
    ModelStepSpec,
    Occurrence,
    OccurrencePosition,
    ToolStepSpec,
    ToolStepReport,
    ModelCost,
    TokenUsage,
    ModelStepReport,
    StepRef,
    value_for_type,
)
from toolang.lang.ast import MapStmt, RunStmt, Span


class _TtyStream(StringIO):
    def isatty(self) -> bool:
        return True


def _run_stmt(runnable: str = "agic:summarize") -> RunStmt:
    return RunStmt(span=Span(line=1), runnable=runnable)


def _model_spec() -> ModelStepSpec:
    return ModelStepSpec(
        setup="test-setup",
        model="test/scripted",
        call=ModelCall(instructions="", messages=[]),
    )


def _execute_part(
    runnable: object = "agic:abc", *, tool_name: str = "runtime__exec"
) -> ToolCallPart:
    return ToolCallPart(
        tool_call_id="execute-1",
        tool_name=tool_name,
        tool_family=tool_name,
        input={"runnable": runnable, "input": {}},
    )


def _execute_step(
    projector: ProgressProjector,
    runnable: str = "agic:abc",
    *,
    error: str | None = None,
    tool_name: str = "runtime__exec",
) -> ProgressUpdate:
    step = StepRef.parse("run_root.1")
    starting = projector.handle(
        step_begin(
            step=step,
            kind="tool",
            spec=ToolStepSpec(
                plugin="runtime",
                call=ToolCall(
                    "execute-1",
                    "execute-1",
                    tool_name,
                    {"runnable": runnable},
                ),
                summary=f"Executing {runnable}...",
            ),
        )
    )
    assert starting.committed == ()
    assert starting.live[0].rows == (
        ProgressRow(
            f"› Executing {runnable}...",
            "active",
            surface="tool_summary",
            tool_identity=(tool_name.split("__", 1)[0], tool_name.split("__", 1)[1]),
            tool_status="running",
        ),
    )
    assert len(starting.live) == 1
    result = ToolResultPart(
        tool_call_id="execute-1",
        tool_name=tool_name,
        tool_family=tool_name,
        error=error,
        output={} if error else {"controls": [f"{step.run_id}@1"]},
    )
    return projector.handle(
        step_end(
            step=step,
            kind="tool",
            status="failed" if error else "succeeded",
            output=Output(value_for_type("ToolResultPart", result), None),
            error=ErrorMessage(error) if error else None,
            report=ToolStepReport(
                summary=f"Failed to execute {runnable}" if error else "Transferred"
            ),
        )
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


@pytest.mark.parametrize("source", ["flow", "tool"])
def test_dynamic_run_projects_a_flat_header_and_child_id_footer(source) -> None:
    projector = ProgressProjector(show_boundaries=False)
    dynamic = StepRef.parse("run_root.0")
    child_model = StepRef.parse("run_child.0")
    projector.handle(
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="agic:parent",
            started_at="2026-01-01T00:00:00Z",
        )
    )

    kind = "run" if source == "flow" else "tool"
    spec = (
        _run_stmt()
        if source == "flow"
        else ToolStepSpec(
            plugin="runtime",
            call=ToolCall(
                "call", "call", "runtime__run", {"runnable": "agic:summarize"}
            ),
            summary="Running agic:summarize",
        )
    )
    starting = projector.handle(
        step_begin(
            step=dynamic, kind=kind, spec=spec, started_at="2026-01-01T00:00:00Z"
        )
    )
    assert starting.committed == ()
    assert starting.live[0].rows == (
        ProgressRow("• Running agic:summarize...", "active")
        if source == "flow"
        else ProgressRow(
            "› Running agic:summarize",
            "active",
            surface="tool_summary",
            tool_identity=("runtime", "run"),
            tool_status="running",
        ),
    )

    header = projector.handle(
        run_begin(
            run="run_child",
            control=ControlRef.for_run("run_child", 0),
            runnable="agic:summarize",
            parent=dynamic,
            started_at="2026-01-01T00:00:00.100Z",
        )
    )
    assert len(header.committed) == 1
    assert header.live == ()
    assert header.committed[0].rows == (
        ProgressRow("Run agic:summarize", leader="run")
        if source == "flow"
        else ProgressRow(
            "Running agic:summarize",
            leader="run",
            tool_identity=("runtime", "run"),
            tool_status="running",
        ),
        ProgressRow(""),
    )
    projector.handle(
        step_begin(
            step=child_model,
            kind="model",
            spec=ModelStepSpec(
                setup="test-setup",
                model="test/scripted",
                call=ModelCall(instructions="", messages=[]),
            ),
            started_at="2026-01-01T00:00:00.200Z",
        )
    )
    projector.handle(
        step_end(
            step=child_model,
            kind="model",
            status="succeeded",
            output=Output(value_for_type("Part[]", (TextPart("summary"),)), "_"),
            report=ModelStepReport(
                usage=TokenUsage(input_tokens=4, output_tokens=2), cost=ModelCost()
            ),
            finished_at="2026-01-01T00:00:01Z",
        )
    )
    projector.handle(
        run_end(
            run="run_child",
            status="succeeded",
            finished_at="2026-01-01T00:00:01.100Z",
        )
    )
    footer = projector.handle(
        step_end(
            step=dynamic,
            kind=kind,
            status="succeeded",
            finished_at="2026-01-01T00:00:02Z",
        )
    )

    assert len(footer.committed) == 1
    row = footer.committed[0].rows[0]
    assert row.text == ""
    assert row.leader == "run"
    assert row.facts == ("2s", "1 run 1 model", "↑4 ↓2")
    assert row.right_status == "succeeded"
    assert row.right_identity == "run_child"
    assert footer.committed[0].rows[-1] == ProgressRow("")


def test_dynamic_run_preaccept_failure_uses_a_trace_marker_without_boundaries() -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepRef.parse("run_root.0")
    projector.handle(
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="agic:parent",
        )
    )
    starting = projector.handle(
        step_begin(
            step=path,
            kind="run",
            spec=_run_stmt("flow:missing"),
        )
    )
    assert starting.committed == ()
    assert starting.live[0].rows == (
        ProgressRow("• Running flow:missing...", "active"),
    )

    terminal = projector.handle(
        step_end(
            step=path,
            kind="run",
            status="failed",
            error=ErrorMessage("Runnable not found: missing"),
        )
    )

    assert terminal.live == ()
    assert terminal.committed[0].rows == (
        ProgressRow("• Failed run flow:missing", "error"),
        ProgressRow("  Runnable not found: missing", "error"),
    )
    rendered = _render_progress(terminal.committed[0], width=72)
    assert rendered.splitlines() == [
        "",
        "• Failed run flow:missing",
        "  Runnable not found: missing",
    ]


def test_execute_projects_a_live_marker_then_a_handoff_header() -> None:
    tool_name = "runtime__exec"
    projector = ProgressProjector(show_boundaries=False)
    caller = StepRef.parse("run_root.0")
    target = StepRef.parse("run_root.2")
    projector.handle(
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="agic:caller",
        )
    )
    projector.handle(step_begin(step=caller, kind="model", spec=_model_spec()))

    starting = projector.handle(
        step_end(
            step=caller,
            kind="model",
            status="succeeded",
            output=Output(
                value_for_type("Part[]", (_execute_part(tool_name=tool_name),)), "_"
            ),
        )
    )

    assert starting.committed == ()
    assert starting.live == ()

    transferred = _execute_step(projector, tool_name=tool_name)
    assert transferred.committed == ()
    assert transferred.live == ()
    header = projector.handle(step_begin(step=target, kind="model", spec=_model_spec()))

    assert header.committed[0].rows == (
        ProgressRow(
            "Transferred",
            leader="handoff",
            tool_identity=("runtime", "exec"),
            tool_status="succeeded",
        ),
        ProgressRow(""),
    )
    assert header.live[0].rows == (ProgressRow("• Thinking", "active"),)
    assert re.search(
        r"- \[.*\] Transferred ─+",
        _render_progress(header.committed[0], width=72),
    )


@pytest.mark.parametrize("width", [1, 4, 5, 16, 72])
def test_handoff_uses_thin_rules_without_losing_its_target(width: int) -> None:
    block = ProgressBlock(
        "handoff",
        (ProgressRow("exec → agic:delegate", leader="handoff"),),
    )
    for render in (_render_progress, _render_chat_progress):
        output = render(block, width=width)
        lines = output.splitlines()
        assert max(display_width(line) for line in lines) <= width
        assert (
            "".join(output.split()).replace("─", "").replace("-", "")
            == "exec→agic:delegate"
        )
        if width == 72:
            assert lines == ["- exec → agic:delegate " + "─" * 49]


@pytest.mark.parametrize("width", [1, 2, 5, 16, 32, 72])
def test_script_and_chat_share_aligned_execution_dividers(width: int) -> None:
    runnable = "agic:summary" if width == 1 else "agic:总结"
    block = ProgressBlock(
        "dividers",
        (
            ProgressRow(f"Run {runnable}", leader="run"),
            ProgressRow(
                "",
                leader="run",
                facts=("2.0s", "1 run"),
                right_status="succeeded",
                right_identity="run_child",
            ),
            ProgressRow("exec → flow:test", leader="handoff"),
            ProgressRow("1/10", leader="iteration"),
        ),
    )
    output = _render_progress(block, width=width)
    assert output == _render_chat_progress(block, width=width)
    lines = output.splitlines()
    assert all(display_width(line) <= width for line in lines)
    compact = "".join(output.split()).translate(str.maketrans("", "", "┌└─·-"))
    assert compact == f"Run{runnable}2.0s1runsucceededrun_childexec→flow:test1/10"
    if width == 72:
        assert [line[:2] for line in lines] == ["┌ ", "└ ", "- ", "──"]
        assert [display_width(line) for line in lines] == [
            width,
            width - 2,
            width,
            width,
        ]
        assert lines[1].endswith("succeeded run_child")


@pytest.mark.parametrize("width", [12, 13, 72])
@pytest.mark.parametrize("caption", ["1/10", "1"])
def test_iteration_caption_is_centered_on_both_surfaces(
    width: int, caption: str
) -> None:
    block = ProgressBlock("iteration", (ProgressRow(caption, leader="iteration"),))
    script = _render_progress(block, width=width)
    assert script == _render_chat_progress(block, width=width)
    (line,) = script.splitlines()
    left, right = line.split(f" {caption} ")
    assert set(left) == set(right) == {"─"}
    assert min(len(left), len(right)) >= 3
    assert abs(len(left) - len(right)) <= 1
    assert display_width(line) == width


@pytest.mark.parametrize("width", [12, 72])
@pytest.mark.parametrize(
    "row",
    [
        ProgressRow("Run agic:hello", leader="run"),
        ProgressRow("exec → flow:test", leader="handoff"),
        ProgressRow("1/10", leader="iteration"),
    ],
)
def test_execution_headers_keep_dim_style(width: int, row: ProgressRow) -> None:
    block = ProgressBlock("header", (row,))
    segments = rendering.render_segments(
        blocks.ExecutionProgressBlock(block, max_width=width).render(), width=width
    )
    captions = [s for s in segments if any(c.isalnum() for c in s.text)]
    assert captions
    for segment in captions:
        assert segment.style is not None
        assert segment.style.dim and not segment.style.bold


def test_execute_uses_its_persisted_running_description() -> None:
    projector = ProgressProjector(show_boundaries=False)
    projector.handle(
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="agic:caller",
        )
    )
    starting = projector.handle(
        step_begin(
            step=StepRef.parse("run_root.0"),
            kind="tool",
            spec=ToolStepSpec(
                plugin="runtime",
                call=ToolCall(
                    "execute-1", "execute-1", "runtime__exec", {"runnable": "next"}
                ),
                summary="Transferring to next...",
            ),
        )
    )
    assert starting.live[0].rows == (
        ProgressRow(
            "› Transferring to next...",
            "active",
            surface="tool_summary",
            tool_identity=("runtime", "exec"),
            tool_status="running",
        ),
    )


@pytest.mark.parametrize("status", ["canceled", "failed"])
def test_uncommitted_execute_uses_its_tool_outcome(
    status: Literal["canceled", "failed"],
) -> None:
    projector = ProgressProjector(show_boundaries=False)
    projector.handle(
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="agic:caller",
        )
    )
    step = StepRef.parse("run_root.0")
    projector.handle(
        step_begin(
            step=step,
            kind="tool",
            spec=ToolStepSpec(
                plugin="runtime",
                call=ToolCall(
                    "execute-1", "execute-1", "runtime__exec", {"runnable": "next"}
                ),
            ),
        )
    )
    summary = "Canceled transfer to next" if status == "canceled" else "Transfer failed"
    ended = projector.handle(
        step_end(
            step=step,
            kind="tool",
            status=status,
            error=ErrorMessage("Execution stopped:\nreason"),
            report=ToolStepReport(summary=summary),
        )
    )
    expected = (
        ProgressRow(
            f"› {summary}",
            "progress",
            surface="tool_summary",
            tool_identity=("runtime", "exec"),
            tool_status=status,
        ),
    )
    if status == "failed":
        expected += (
            ProgressRow("  Execution stopped: reason", "error", surface="tool_error"),
        )
    assert ended.committed[0].rows == expected
    assert ended.live == ()


@pytest.mark.parametrize("status", ["succeeded", "canceled", "failed"])
def test_confirmed_execute_without_target_step_is_not_reported_as_failed(
    status: Literal["succeeded", "canceled", "failed"],
) -> None:
    projector = ProgressProjector(show_boundaries=False)
    projector.handle(
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="agic:caller",
        )
    )
    assert _execute_step(projector).committed == ()
    ended = projector.handle(run_end(run="run_root", status=status))
    rows = tuple(row for block in ended.committed for row in block.rows)
    assert rows == (
        ProgressRow(
            "Transferred",
            leader="handoff",
            tool_identity=("runtime", "exec"),
            tool_status="succeeded",
        ),
        ProgressRow(""),
    )
    assert not projector._broken


@pytest.mark.parametrize("status", ["succeeded", "canceled", "failed"])
@pytest.mark.parametrize("next_tool", [None, "read", "honor"])
def test_confirmed_execute_stays_in_its_parallel_lane(
    status: Literal["succeeded", "canceled", "failed"],
    next_tool: str | None,
) -> None:
    projector = ProgressProjector(show_boundaries=False)
    par = StepRef.parse("run_root.0")
    step = StepRef.parse("run_worker.0")
    projector.handle(
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="flow:parent",
        )
    )
    projector.handle(
        step_begin(
            step=par,
            kind="par",
            spec=MapStmt(span=Span(line=1), runnable="worker", lanes=1),
        )
    )
    projector.handle(
        run_begin(
            run="run_worker",
            parent=par,
            control=ControlRef.for_run("run_worker", 0),
            runnable="agic:worker",
            occurrence=Occurrence(
                item=OccurrencePosition(index=0, count=1),
                lane=OccurrencePosition(index=0, count=1),
            ),
        )
    )
    projector.handle(
        step_begin(
            step=step,
            kind="tool",
            spec=ToolStepSpec(
                plugin="runtime",
                call=ToolCall(
                    "execute-1", "execute-1", "runtime__exec", {"runnable": "next"}
                ),
                summary="Transferring to next...",
            ),
        )
    )
    transferred = projector.handle(
        step_end(
            step=step,
            kind="tool",
            status="succeeded",
            report=ToolStepReport(summary="Transferred to next"),
            output=Output(
                value_for_type(
                    "ToolResultPart",
                    ToolResultPart(
                        tool_call_id="execute-1",
                        call_id="execute-1",
                        tool_name="runtime__exec",
                        tool_family="runtime__exec",
                        output={"controls": ["run_worker@1"]},
                    ),
                )
            ),
        )
    )
    if next_tool is not None:
        plugin = "fs" if next_tool == "read" else "runtime"
        marker = "›" if next_tool == "read" else "✧"
        summary = (
            "Reading repo://file..." if next_tool == "read" else "Loading rules..."
        )
        target = StepRef.parse("run_worker.1")
        started = projector.handle(
            step_begin(
                step=target,
                kind="tool",
                spec=ToolStepSpec(
                    plugin=plugin,
                    call=ToolCall("target", "target", f"{plugin}__{next_tool}", {}),
                    summary=summary,
                ),
            )
        )
        row = started.live[0].rows[-1]
        assert row.text == f"  0 | #0 | {marker} Handoff to next · {summary}"
        assert row.tone == "active" and row.surface == "tool_summary"
        completed = projector.handle(
            step_end(
                step=target,
                kind="tool",
                status="succeeded",
                report=ToolStepReport(summary="Finished"),
            )
        )
        assert completed.live[0].rows[-1].text == f"  0 | #0 | {marker} Finished"
        assert completed.live[0].rows[-1].tone == "progress"
    ended = projector.handle(run_end(run="run_worker", status=status))

    assert transferred.committed == ended.committed == ()
    assert transferred.live[0].rows[-1].text == "  0 | #0 | › Transferred to next"
    assert not projector._broken


def test_execute_prestart_failure_uses_a_correlated_trace_marker() -> None:
    projector = ProgressProjector(show_boundaries=False)
    caller = StepRef.parse("run_root.0")
    recovery = StepRef.parse("run_root.2")
    projector.handle(
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="agic:caller",
        )
    )
    projector.handle(step_begin(step=caller, kind="model", spec=_model_spec()))
    projector.handle(
        step_end(
            step=caller,
            kind="model",
            status="succeeded",
            output=Output(
                value_for_type("Part[]", (_execute_part("flow:missing"),)), "_"
            ),
        )
    )

    failed = _execute_step(
        projector, "flow:missing", error="Runnable not found:\nmissing"
    )

    assert failed.committed[0].rows == (
        ProgressRow(
            "› Failed to execute flow:missing",
            "progress",
            surface="tool_summary",
            tool_identity=("runtime", "exec"),
            tool_status="failed",
        ),
        ProgressRow("  Runnable not found: missing", "error", surface="tool_error"),
    )
    assert failed.live == ()
    recovered = projector.handle(
        step_begin(step=recovery, kind="model", spec=_model_spec())
    )
    assert recovered.committed == ()
    assert recovered.live[0].rows == (ProgressRow("• Thinking", "active"),)


def test_handoff_to_flow_keeps_the_first_run_statement_flow_owned() -> None:
    projector = ProgressProjector()
    caller = StepRef.parse("run_root.0")
    target = StepRef.parse("run_root.2")
    projector.handle(
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="agic:caller",
        )
    )
    projector.handle(step_begin(step=caller, kind="model", spec=_model_spec()))
    projector.handle(
        step_end(
            step=caller,
            kind="model",
            status="succeeded",
            output=Output(
                value_for_type("Part[]", (_execute_part("flow:delegate"),)), "_"
            ),
        )
    )

    assert _execute_step(projector, "flow:delegate").committed == ()
    header = projector.handle(
        step_begin(
            step=target,
            kind="run",
            spec=_run_stmt("agic:worker"),
        )
    )

    assert header.live == ()
    assert header.committed[0].rows == (
        ProgressRow(
            "Transferred",
            leader="handoff",
            tool_identity=("runtime", "exec"),
            tool_status="succeeded",
        ),
        ProgressRow(""),
        ProgressRow("[0] Run agic:worker"),
        ProgressRow(""),
    )


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
                            "Run agic:summarize",
                            leader="run",
                        ),
                        ProgressRow(
                            "",
                            leader="run",
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
    assert wide_lines[0].startswith("┌ Run agic:summarize ───")
    assert wide_lines[1].startswith("└ 2.0s · 1 run · 1 model call ───")
    assert wide_lines[1].endswith("succeeded run_abc123")
    assert [display_width(line) for line in wide_lines] == [72, 70]

    narrow = StringIO()
    ProgressConsole(narrow, width=32).apply(
        ProgressUpdate(
            committed=(
                ProgressBlock(
                    "dynamic",
                    (
                        ProgressRow(
                            "",
                            leader="run",
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
                            "Run agic:总结",
                            leader="run",
                        ),
                        ProgressRow(
                            "",
                            leader="run",
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
    assert "Runagic:总结" in compact
    assert "2.0s1run" in compact
    assert "succeededrun_完整" in compact
    assert all(display_width(line) <= 5 for line in tiny_lines)


def test_flow_owned_run_step_keeps_numbered_header_and_step_path_footer() -> None:
    projector = ProgressProjector()
    step = StepRef.parse("run_root.0")
    projector.handle(
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="flow:publish",
        )
    )

    header = projector.handle(
        step_begin(step=step, kind="run", spec=_run_stmt("summarize"))
    )
    assert header.committed[0].rows == (
        ProgressRow("[0] Run summarize"),
        ProgressRow(""),
    )

    projector.handle(
        run_begin(
            run="run_child",
            control=ControlRef.for_run("run_child", 0),
            runnable="agic:summarize",
            parent=step,
        )
    )
    projector.handle(run_end(run="run_child", status="succeeded"))
    terminal = projector.handle(step_end(step=step, kind="run", status="succeeded"))

    footer = terminal.committed[0].rows[-2]
    assert footer.right_text == "run_root.0"
    assert footer.right_status == ""
    assert footer.right_identity == ""
    assert all(row.leader == "none" for row in terminal.committed[0].rows)


def test_agic_to_flow_keeps_child_flow_run_steps_numbered() -> None:
    projector = ProgressProjector()
    dynamic = StepRef.parse("run_root.0")
    authored = StepRef.parse("run_publish.0")
    projector.handle(
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="agic:parent",
        )
    )
    projector.handle(
        step_begin(
            step=dynamic,
            kind="run",
            spec=_run_stmt("flow:publish"),
        )
    )
    outer_header = projector.handle(
        run_begin(
            run="run_publish",
            control=ControlRef.for_run("run_publish", 0),
            runnable="flow:publish",
            parent=dynamic,
        )
    )

    flow_header = projector.handle(
        step_begin(
            step=authored,
            kind="run",
            spec=_run_stmt("validate"),
        )
    )
    projector.handle(
        run_begin(
            run="run_validate",
            control=ControlRef.for_run("run_validate", 0),
            runnable="agic:validate",
            parent=authored,
        )
    )
    projector.handle(run_end(run="run_validate", status="succeeded"))
    flow_footer = projector.handle(
        step_end(step=authored, kind="run", status="succeeded")
    )
    projector.handle(run_end(run="run_publish", status="succeeded"))
    outer_footer = projector.handle(
        step_end(step=dynamic, kind="run", status="succeeded")
    )

    assert outer_header.committed[0].rows[0] == ProgressRow(
        "Run flow:publish",
        leader="run",
    )
    assert flow_header.committed[0].rows[0].text == "[0] Run validate"
    assert flow_footer.committed[0].rows[-2].right_text == "run_publish.0"
    assert outer_footer.committed[0].rows[0].right_identity == "run_publish"


def test_nested_dynamic_run_footers_pair_with_their_direct_children() -> None:
    projector = ProgressProjector(show_boundaries=False)
    outer = StepRef.parse("run_root.0")
    inner = StepRef.parse("run_child.0")
    projector.handle(
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="agic:parent",
        )
    )
    projector.handle(step_begin(step=outer, kind="run", spec=_run_stmt("agic:child")))
    outer_header = projector.handle(
        run_begin(
            run="run_child",
            control=ControlRef.for_run("run_child", 0),
            runnable="agic:child",
            parent=outer,
        )
    )
    projector.handle(step_begin(step=inner, kind="run", spec=_run_stmt("agic:leaf")))
    inner_header = projector.handle(
        run_begin(
            run="run_leaf",
            control=ControlRef.for_run("run_leaf", 0),
            runnable="agic:leaf",
            parent=inner,
        )
    )
    projector.handle(run_end(run="run_leaf", status="succeeded"))
    inner_footer = projector.handle(
        step_end(step=inner, kind="run", status="succeeded")
    )
    projector.handle(run_end(run="run_child", status="succeeded"))
    outer_footer = projector.handle(
        step_end(step=outer, kind="run", status="succeeded")
    )

    assert outer_header.committed[0].rows[0].text == "Run agic:child"
    assert inner_header.committed[0].rows[0].text == "Run agic:leaf"
    assert inner_footer.committed[0].rows[0].right_identity == "run_leaf"
    assert outer_footer.committed[0].rows[0].right_identity == "run_child"
    assert all(
        row.leader == "run"
        for row in (
            outer_header.committed[0].rows[0],
            inner_header.committed[0].rows[0],
            inner_footer.committed[0].rows[0],
            outer_footer.committed[0].rows[0],
        )
    )


def test_dynamic_run_inside_parallel_lane_stays_on_one_lane_row() -> None:
    projector = ProgressProjector()
    parallel = StepRef.parse("run_root.0")
    dynamic = StepRef.parse("run_child.0")
    projector.handle(
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="flow:batch",
        )
    )
    projector.handle(
        step_begin(
            step=parallel,
            kind="par",
            spec=MapStmt(span=Span(line=1), runnable="worker", lanes=1),
        )
    )
    projector.handle(
        run_begin(
            run="run_child",
            control=ControlRef.for_run("run_child", 0),
            runnable="agic:worker",
            parent=parallel,
            occurrence=Occurrence(
                item=OccurrencePosition(index=0, count=1),
                lane=OccurrencePosition(index=0, count=1),
            ),
        )
    )

    update = projector.handle(
        step_begin(step=dynamic, kind="run", spec=_run_stmt("agic:leaf"))
    )

    rows = update.live[0].rows
    assert len(rows) == 2
    assert rows[1].text.endswith("• Running agic:leaf...")
    assert all(row.leader == "none" for row in rows)

    projector.handle(
        run_begin(
            run="run_leaf",
            control=ControlRef.for_run("run_leaf", 0),
            runnable="agic:leaf",
            parent=dynamic,
        )
    )
    projector.handle(run_end(run="run_leaf", status="succeeded"))
    terminal = projector.handle(step_end(step=dynamic, kind="run", status="succeeded"))

    assert terminal.committed == ()
    assert terminal.live[0].rows[1].text.endswith("• Completed agic:leaf")
    assert all(row.leader == "none" for row in terminal.live[0].rows)


@pytest.mark.parametrize(
    ("status", "status_color"),
    [
        ("succeeded", None),
        ("failed", "red"),
        ("canceled", "yellow"),
    ],
)
def test_dynamic_footer_colors_only_the_terminal_status(
    status: str,
    status_color: str | None,
) -> None:
    block = ProgressBlock(
        "dynamic",
        (
            ProgressRow(
                "",
                leader="run",
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

    marker = next(segment for segment in segments if "└" in segment.text)
    border = next(segment for segment in segments if "─" in segment.text)
    facts = next(segment for segment in segments if "2.0s" in segment.text)
    terminal = next(segment for segment in segments if status in segment.text)
    identity = next(segment for segment in segments if "run_child" in segment.text)
    for segment in (marker, border, facts, identity):
        assert segment.style is not None
        assert segment.style.dim
        assert segment.style.color is None
    assert terminal.style is not None
    if status_color is None:
        assert terminal.style.dim
        assert terminal.style.color is None
    else:
        assert terminal.style.color is not None
        assert terminal.style.color.name == status_color


def test_dynamic_scope_suppresses_internal_call_and_protocol_result_rows() -> None:
    projector = ProgressProjector(show_boundaries=False)
    model = StepRef.parse("run_root.0")
    dynamic = StepRef.parse("run_root.1")
    projector.handle(
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="agic:parent",
        )
    )
    projector.handle(step_begin(step=model, kind="model", spec=_model_spec()))
    hidden = projector.handle(
        step_end(
            step=model,
            kind="model",
            status="succeeded",
            output=Output(
                value_for_type(
                    "Part[]",
                    (
                        ToolCallPart(
                            tool_call_id="runtime-call",
                            tool_name="_internal_run_action",
                            tool_family="runtime",
                            input={"runnable": "agic:child"},
                        ),
                    ),
                ),
                "_",
            ),
        )
    )
    projector.handle(step_begin(step=dynamic, kind="run", spec=_run_stmt("agic:child")))
    header = projector.handle(
        run_begin(
            run="run_child",
            control=ControlRef.for_run("run_child", 0),
            runnable="agic:child",
            parent=dynamic,
        )
    )
    projector.handle(run_end(run="run_child", status="succeeded"))
    footer = projector.handle(
        step_end(
            step=dynamic,
            kind="run",
            status="succeeded",
            output=Output(
                value_for_type(
                    "Part[]",
                    (
                        ToolResultPart(
                            tool_call_id="runtime-call",
                            tool_name="_internal_run_action",
                            tool_family="runtime",
                            output={"run": "run_child"},
                        ),
                    ),
                ),
                "_",
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
    assert "Run agic:child" in visible


def test_dynamic_header_normalizes_untrusted_runnable_text() -> None:
    projector = ProgressProjector(show_boundaries=False)
    path = StepRef.parse("run_root.0")
    projector.handle(
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="agic:parent",
        )
    )

    projector.handle(
        step_begin(
            step=path,
            kind="run",
            spec=_run_stmt("flow:\x00missing\nname" + "x" * 300),
        )
    )
    header = projector.handle(
        run_begin(
            run="run_child",
            control=ControlRef.for_run("run_child", 0),
            runnable="flow:missing",
            parent=path,
        )
    )
    caption = header.committed[0].rows[0].text

    assert caption.startswith("Run flow: missing name")
    assert "\x00" not in caption
    assert "\n" not in caption
    assert len(caption.removeprefix("Run ")) == 240


def test_script_and_chat_render_dynamic_boundaries_identically() -> None:
    progress = ProgressBlock(
        "dynamic",
        (
            ProgressRow("Run agic:summarize", leader="run"),
            ProgressRow(""),
            ProgressRow("• Summary", "normal"),
            ProgressRow(""),
            ProgressRow(
                "",
                leader="run",
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
            ProgressRow("Run agic:summarize", leader="run"),
            ProgressRow(""),
            ProgressRow("• Summary", "normal"),
            ProgressRow(""),
            ProgressRow(
                "",
                leader="run",
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
    assert re.search(r"┌ Run agic:summarize ─+\n\n• Summary", rendered)
    assert re.search(r"• Summary\n\n└ 2.0s", rendered)
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
        "▪︎ run_root123 succeeded   8.2s · 4 runs · 6 model calls · 2 tool calls\n"
    )
