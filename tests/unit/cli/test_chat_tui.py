from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from io import StringIO
import threading
from types import SimpleNamespace
from typing import Any, Literal, cast

from prompt_toolkit.layout import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.processors import AfterInput, ConditionalProcessor
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.utils import get_cwidth
from rich.console import RenderableType
from rich.text import Text
import pytest

from toolang.base.types.message import (
    Part,
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.base.types.run import ModelCall, ToolCall
from toolang.cli.toolang.commands.chat import (
    blocks,
    events,
    rendering,
    slashes,
    tui,
    widgets,
)
from toolang.cli.toolang.commands.chat.policy import apply_session_commands
from toolang.cli.toolang.commands.chat.events import ChatUIEvent
from toolang.cli.toolang.commands.chat.base import ChatClient, ChatResult, QueuedCall
from toolang.cli.toolang.commands.chat.presenter import ChatRunPresenter
from toolang.cli.common.execution_progress import (
    ProgressBlock,
    ProgressRow,
    ProgressUpdate,
)
from toolang.cli.common.script_progress.console import ProgressConsole
from toolang.cli.common.execution_progress.state import Metrics
from toolang.execution.events import (
    PartBegin,
    PartDelta,
    PartEnd,
    RunBegin,
    RunEnd,
    RunEvent,
    StepBegin,
    StepEnd,
)
from toolang.execution.types import (
    ControlRef,
    Local,
    ModelStepGiven,
    ModelStepNoted,
    ModelTokenCount,
    Occurrence,
    OccurrencePosition,
    RunOverride,
    StepPath,
    Pointer,
    ToolStepGiven,
)
from toolang.lang.ast import MapStmt, RunStmt, Span
from tests.support import chat_tui_pty


def _parts(*parts: Part) -> Local:
    return Local.typed("Part[]", tuple(parts), "_", 0)


def _output(step: StepPath) -> Local:
    return Local.typed("Part[]", Pointer.step(step), "_", 0)


def _model_given(model: str = "test/model") -> ModelStepGiven:
    return ModelStepGiven(model=model, call=ModelCall(instructions="", messages=[]))


def _tool_given(name: str = "shell__execute") -> ToolStepGiven:
    return ToolStepGiven(
        plugin="shell",
        call=ToolCall(
            tool_call_id="call_1",
            call_id="call_1",
            name=name,
            input={"command": "echo ok"},
        ),
    )


def test_chat_tui_pty_treats_linux_eio_as_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = cast(Any, SimpleNamespace(poll=lambda: None))
    session = chat_tui_pty.ChatTuiPtySession(master=123, process=process)
    monkeypatch.setattr(
        chat_tui_pty.select,
        "select",
        lambda *_args: ([session.master], [], []),
    )

    def closed_pty(*_args: object) -> bytes:
        raise OSError(chat_tui_pty.errno.EIO, "pty closed")

    monkeypatch.setattr(chat_tui_pty.os, "read", closed_pty)

    session._read(timeout=0)

    assert session.data == b""


def test_chat_run_begin_finalizes_local_submission_block() -> None:
    app = FakeApp()
    app.live_blocks.append(blocks.RunStartBlock.create("hello"))

    assert [block.type for block in app.live_blocks] == ["RunStartBlock"]
    assert "hello" in _render_text(app.live_blocks[0].render())

    events.handle_run_event(_run_begin(), app)

    assert [block.type for block in app.live_blocks] == ["RunStopBlock"]
    assert [block.type for block in app.finalized] == ["RunStartBlock"]
    assert "run_1" not in _render_text(app.finalized[0].render())


def test_chat_uses_shared_progress_blocks_for_live_and_finalized_model_output() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(), app)
    events.handle_run_event(_model_step_begin(), app)
    assert [block.type for block in app.live_blocks] == [
        "ExecutionProgressBlock",
        "RunStopBlock",
    ]
    assert "• thinking" in _render_text(app.live_blocks[0].render())

    events.handle_run_event(
        PartBegin(
            step=StepPath.parse("run_1.1"),
            part=0,
            part_type="text",
        ),
        app,
    )
    events.handle_run_event(
        PartDelta(
            step=StepPath.parse("run_1.1"),
            part=0,
            delta=TextDelta(text="drafting"),
        ),
        app,
    )
    streamed = _render_text(app.live_blocks[0].render())
    assert "• drafting" in streamed
    assert "thinking" not in streamed
    events.handle_run_event(
        PartEnd(
            step=StepPath.parse("run_1.1"),
            part=0,
            data=TextPart("drafting"),
        ),
        app,
    )
    events.handle_run_event(_model_step_end(output="drafting"), app)

    assert [block.type for block in app.live_blocks] == ["RunStopBlock"]
    assert [block.type for block in app.finalized] == ["ExecutionProgressBlock"]
    rendered = _render_text(app.finalized[0].render())
    assert "• drafting" in rendered
    assert "run_1.1" not in rendered
    assert "test/model" not in rendered
    output_segment = next(
        segment
        for segment in rendering.render_segments(
            app.finalized[0].render(),
            width=80,
        )
        if "drafting" in segment.text
    )
    assert output_segment.style is None or not output_segment.style.dim

    events.handle_run_event(_run_end(status="succeeded", output_step_index=1), app)
    assert [block.type for block in app.finalized] == [
        "ExecutionProgressBlock",
        "RunStopBlock",
    ]
    transcript = "".join(_render_text(block.render()) for block in app.finalized)
    assert "• drafting\n\n• run_1 succeeded" in transcript


def test_chat_flow_keeps_one_blank_row_at_each_finalized_boundary() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(executable_kind="flow"), app)
    events.handle_run_event(_flow_step_begin(), app)
    events.handle_run_event(_flow_step_end(), app)
    events.handle_run_event(_run_end(status="succeeded", output_step_index=1), app)

    transcript = "".join(_render_text(block.render()) for block in app.finalized)
    assert "[1] Run summarize for each item, up to 2 at once\n\n• Mapped" in (
        transcript
    )
    assert "items in parallel\n\n• run_1 succeeded" in transcript
    assert "items in parallel\n\n\n• run_1 succeeded" not in transcript


def test_chat_moves_stable_markdown_to_scrollback_while_the_tail_stays_live() -> None:
    app = FakeApp()
    path = StepPath.parse("run_1.1")

    events.handle_run_event(_run_begin(), app)
    events.handle_run_event(_model_step_begin(), app)
    events.handle_run_event(
        PartBegin(step=path, part=0, part_type="text"),
        app,
    )
    events.handle_run_event(
        PartDelta(step=path, part=0, delta=TextDelta("# Heading\n\n")),
        app,
    )
    assert app.finalized == []
    assert _render_text(app.live_blocks[0].render()).startswith("• Heading")

    events.handle_run_event(
        PartDelta(step=path, part=0, delta=TextDelta("Paragraph")),
        app,
    )
    assert len(app.finalized) == 1
    assert _render_text(app.finalized[0].render()).startswith("• Heading")
    assert _render_text(app.live_blocks[0].render()).splitlines() == [
        "",
        "  Paragraph",
    ]

    events.handle_run_event(
        PartEnd(
            step=path,
            part=0,
            data=TextPart("# Heading\n\nParagraph"),
        ),
        app,
    )
    assert len(app.finalized) == 2
    assert _render_text(app.finalized[1].render()).splitlines() == [
        "",
        "  Paragraph",
    ]
    assert [block.type for block in app.live_blocks] == ["RunStopBlock"]

    events.handle_run_event(
        _model_step_end(output="# Heading\n\nParagraph"),
        app,
    )
    assert len(app.finalized) == 2


def test_chat_parallel_terminal_update_replaces_every_lane_atomically() -> None:
    app = FakeApp()
    par_path = StepPath.parse("run_1.1")

    events.handle_run_event(_run_begin(executable_kind="flow"), app)
    events.handle_run_event(
        StepBegin(
            step=par_path,
            kind="par",
            given=MapStmt(span=Span(line=1), runnable="summarize", lanes=2),
        ),
        app,
    )
    for item in range(2):
        events.handle_run_event(
            RunBegin(
                run=f"run_child_{item}",
                parent=par_path,
                control=ControlRef(f"run_child_{item}", 0),
                runnable="agic:summarize",
                occurrence=Occurrence(
                    item=OccurrencePosition(index=item, count=2),
                    lane=OccurrencePosition(index=item, count=2),
                ),
            ),
            app,
        )
        events.handle_run_event(
            _model_step_begin(run_id=f"run_child_{item}", step_index=0),
            app,
        )

    live = _render_text(app.live_blocks[0].render())
    assert "0 | #0 | • thinking" in live
    assert "1 | #1 | • thinking" in live

    events.handle_run_event(
        StepEnd(
            step=StepPath.parse("run_child_0.0"),
            kind="model",
            status="failed",
            error="model unavailable",
        ),
        app,
    )
    events.handle_run_event(
        RunEnd(
            run="run_child_0",
            status="failed",
            error=Pointer.step(StepPath.parse("run_child_0.0")),
        ),
        app,
    )
    events.handle_run_event(
        StepEnd(
            step=StepPath.parse("run_child_1.0"),
            kind="model",
            status="canceled",
        ),
        app,
    )
    events.handle_run_event(RunEnd(run="run_child_1", status="canceled"), app)
    events.handle_run_event(
        StepEnd(
            step=par_path,
            kind="par",
            status="failed",
            error="parallel step stopped because lane 0 (#0) failed",
        ),
        app,
    )

    assert [block.type for block in app.live_blocks] == ["RunStopBlock"]
    finalized = _render_text(app.finalized[-1].render())
    assert (
        "• Parallel execution stopped: 0/2 succeeded, 1 failed, and 1 was canceled"
    ) in finalized
    assert "parallel step stopped because lane 0 (#0) failed" in finalized
    assert "0 | #0 | • failed model unavailable" in finalized


@pytest.mark.parametrize(
    "rows",
    [
        (ProgressRow("• executed web_search.search"), ProgressRow("  5 results")),
        (ProgressRow("[0] Run summarize"), ProgressRow("")),
    ],
)
def test_script_and_chat_sinks_preserve_the_same_semantic_rows(
    rows: tuple[ProgressRow, ...],
) -> None:
    progress = ProgressBlock("step:run_1.0", rows)
    stream = StringIO()
    ProgressConsole(stream).apply(ProgressUpdate(committed=(progress,)))
    chat = blocks.ExecutionProgressBlock(progress)

    assert stream.getvalue() == _render_text(chat.render())


def test_chat_submission_has_no_status_before_run_begin() -> None:
    block = blocks.RunStartBlock.create("hello")

    rendered = _render_text(block.render(), width=20)

    assert "▌" not in rendered
    assert "  hello" in rendered
    assert ">" not in rendered
    assert "starting" not in rendered
    control_line = "  hello" + " " * 13
    blank_control_line = " " * 20
    assert rendered.splitlines() == [
        blank_control_line,
        control_line,
        blank_control_line,
        "",
    ]


def test_chat_preaccept_error_does_not_render_a_failed_run() -> None:
    app = FakeApp()
    app.live_blocks.append(blocks.RunStartBlock.create(":flow missing\n\nInput"))

    handled = events.handle_run_error(app, "Runnable not found: missing")

    assert handled is True
    assert app.live_blocks == []
    assert [block.type for block in app.finalized] == [
        "RunStartBlock",
        "SubmissionErrorBlock",
    ]
    rendered = "\n".join(_render_text(block.render()) for block in app.finalized)
    assert "• Runnable not found: missing" in rendered
    assert "starting" not in rendered
    assert "run failed" not in rendered
    assert app.finished


def test_chat_local_stop_updates_existing_run_stop_block() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(), app)
    stop = cast(blocks.RunStopBlock, app.live_blocks[0])
    stop.mark_canceling()

    assert [block.type for block in app.live_blocks] == ["RunStopBlock"]
    assert "canceling" in _render_text(app.live_blocks[0].render())


def test_chat_run_stop_block_shows_canceling_then_canceled() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(), app)
    stop = cast(blocks.RunStopBlock, app.live_blocks[0])
    stop.mark_canceling()

    assert [block.type for block in app.live_blocks] == ["RunStopBlock"]
    assert "canceling" in _render_text(app.live_blocks[0].render())

    events.handle_run_event(_run_end(status="canceled"), app)

    assert app.live_blocks == []
    assert [block.type for block in app.finalized] == ["RunStopBlock"]
    rendered = _render_text(app.finalized[0].render())
    lines = rendered.splitlines()
    assert lines[0] == ""
    assert lines[1].startswith("• run_1 canceled ")
    assert lines[2].startswith("  3.0s")
    assert lines[3] == ""


def test_chat_root_footer_counts_child_runs_for_any_runnable_kind() -> None:
    block = blocks.RunStopBlock.create(_run_begin(), max_width=72)
    block.update(_run_end(status="succeeded"))
    block.set_metrics(
        Metrics(
            runs=7,
            model_calls=8,
            tool_calls=2,
            input_tokens=1200,
            output_tokens=300,
        )
    )

    rendered = _render_text(block.render(), width=160)
    lines = [line for line in rendered.splitlines() if line]

    assert "run_1 succeeded" in rendered
    assert "6 runs" in rendered
    assert "8 model calls" in rendered
    assert "2 tool calls" in rendered
    assert len(lines[0]) == 42
    assert len(lines[1]) > len(lines[0])
    assert lines[0].index("run_1") == 2
    assert lines[1].index("3.0s") == 2


def test_chat_root_footer_omits_zero_child_runs() -> None:
    block = blocks.RunStopBlock.create(_run_begin())
    block.update(_run_end(status="succeeded"))
    block.set_metrics(Metrics(runs=1))

    rendered = _render_text(block.render())

    assert "0 runs" not in rendered


def test_chat_root_footer_uses_a_fixed_divider_width_for_short_facts() -> None:
    block = blocks.RunStopBlock.create(_run_begin(run_id="run_pmqv7gfc"))
    block.update(_run_end(run_id="run_pmqv7gfc", status="succeeded"))

    lines = [line for line in _render_text(block.render()).splitlines() if line]
    prefix = "• run_pmqv7gfc succeeded "

    assert lines[0] == prefix + "─" * (42 - len(prefix))


def test_chat_root_footer_wraps_every_facts_line_at_the_step_text_indent() -> None:
    block = blocks.RunStopBlock.create(_run_begin(), max_width=32)
    block.update(_run_end(status="failed"))
    block.set_metrics(
        Metrics(
            runs=7,
            model_calls=8,
            tool_calls=2,
            input_tokens=1200,
            output_tokens=300,
        )
    )

    lines = [
        line for line in _render_text(block.render(), width=160).splitlines() if line
    ]

    assert len(lines[0]) == 32
    assert lines[0].startswith("• run_1 failed ")
    assert all(line.startswith("  ") for line in lines[1:])
    assert all(not line.startswith(("│", "└")) for line in lines[1:])


def test_chat_tool_step_uses_bullet_marker_and_summary() -> None:
    block = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_1.1",
            (ProgressRow("• executing shell__execute", "active"),),
        )
    )

    running_segments = [
        segment
        for segment in rendering.render_segments(block.render(), width=80)
        if segment.text.strip()
    ]
    assert running_segments
    assert "• executing shell__execute" in _render_text(block.render())
    assert all(
        segment.style is None or not segment.style.dim for segment in running_segments
    )

    block.update(
        ProgressBlock(
            "step:run_1.1",
            (
                ProgressRow("• executed shell__execute"),
                ProgressRow("  ok"),
            ),
        )
    )
    rendered = _render_text(block.render())
    finalized_segments = [
        segment
        for segment in rendering.render_segments(block.render(), width=80)
        if segment.text.strip()
    ]

    assert rendered.startswith("• executed shell__execute")
    assert "ok" in rendered
    assert all(
        segment.style is not None and segment.style.dim
        for segment in finalized_segments
    )


def test_chat_truncates_live_lane_but_preserves_its_finalized_output() -> None:
    row = ProgressRow(
        "  0 | #0 | • failed " + "provider returned a complete long diagnostic",
        "error",
    )

    live = _render_text(
        blocks.ExecutionProgressBlock(
            ProgressBlock("par:run_1.0", (row,)),
            live=True,
        ).render(),
        width=32,
    )
    finalized = _render_text(
        blocks.ExecutionProgressBlock(
            ProgressBlock("par:run_1.0", (row,)),
        ).render(),
        width=10,
    )

    assert "complete long diagnostic" not in live
    assert "…" in live
    assert "complete long diagnostic" in " ".join(finalized.split())


def test_chat_wraps_trace_model_activity_across_live_rows() -> None:
    content = "first streamed line second streamed line third streamed line"
    block = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_1.0",
            (
                ProgressRow(
                    f"• {content}",
                    "active",
                    wrap_live=True,
                ),
            ),
        ),
        live=True,
    )

    rendered = _render_text(block.render(), width=32)

    lines = rendered.splitlines()
    assert len(lines) > 1
    assert rendered.startswith("• first")
    assert "..." not in rendered
    assert " ".join(line.strip().removeprefix("• ") for line in lines) == content


def test_chat_progress_width_is_bounded_on_a_wide_terminal() -> None:
    content = " ".join(f"word{index}" for index in range(40))
    block = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_1.0",
            (ProgressRow(f"• {content}"),),
        )
    )

    rendered = _render_text(block.render(), width=160)
    lines = rendered.splitlines()

    assert all(rendering.display_len(line) <= 120 for line in lines)
    assert " ".join(line.strip().removeprefix("• ") for line in lines) == content


def test_chat_progress_width_honors_configured_maximum() -> None:
    content = " ".join(f"word{index}" for index in range(20))
    block = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_1.0",
            (ProgressRow(f"• {content}"),),
        ),
        max_width=48,
    )

    rendered = _render_text(block.render(), width=160)
    lines = rendered.splitlines()

    assert all(rendering.display_len(line) <= 48 for line in lines)
    assert " ".join(line.strip().removeprefix("• ") for line in lines) == content


def test_chat_wraps_finalized_parallel_lane_at_its_embedded_marker() -> None:
    block = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "par:run_1.0",
            (
                ProgressRow(
                    "  1 | #5 | • failed provider returned a complete long diagnostic",
                    "error",
                ),
                ProgressRow(
                    "             retry after sixty seconds and contact the provider",
                    "error",
                ),
            ),
        )
    )

    rendered = _render_text(block.render(), width=40)

    assert rendered.splitlines() == [
        "  1 | #5 | • failed provider returned a",
        "             complete long diagnostic",
        "             retry after sixty seconds",
        "             and contact the provider",
    ]


def test_chat_canceled_model_step_is_not_rendered_as_completed() -> None:
    block = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_1.1",
            (ProgressRow("• canceled", "warning"),),
        )
    )

    rendered = _render_text(block.render())

    assert "• canceled" in rendered
    assert "model completed" not in rendered


def test_late_root_begin_does_not_replace_a_different_active_run() -> None:
    app = FakeApp(active_run="run_new")

    events.handle_run_event(_run_begin(run_id="run_old"), app)

    assert app.active_run == "run_new"


def test_run_event_guard_rejects_unrelated_typed_values() -> None:
    assert not tui._is_run_event(SimpleNamespace(type="run_end"))


def test_chat_canceled_statement_uses_one_diagnostic_and_continuation_facts() -> None:
    block = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "par:run_1.2",
            (
                ProgressRow("• 5 succeeded · 1 canceled", "warning"),
                ProgressRow("  27.0s · 5 runs", "progress"),
            ),
        )
    )

    rendered = _render_text(block.render())

    assert "• 5 succeeded · 1 canceled" in rendered
    assert "statement failed" not in rendered
    assert "  27.0s · 5 runs" in rendered


@pytest.mark.parametrize(
    ("status", "color", "dim"),
    [
        ("succeeded", None, True),
        ("failed", "red", False),
        ("canceled", "yellow", False),
    ],
)
def test_chat_run_footer_styles_marker_caption_rule_and_facts(
    status: Literal["succeeded", "failed", "canceled"],
    color: str | None,
    dim: bool,
) -> None:
    root_summary = blocks.RunStopBlock.create(_run_begin())
    root_summary.update(_run_end(status=status))
    segments = [
        segment
        for segment in rendering.render_segments(root_summary.render(), width=80)
        if segment.text.strip()
    ]

    def assert_status_style(segment: Any) -> None:
        assert segment.style is not None
        assert bool(segment.style.dim) is dim
        if color is None:
            assert segment.style.color is None
        else:
            assert segment.style.color is not None
            assert segment.style.color.name == color

    marker = next(segment for segment in segments if segment.text == "•")
    assert_status_style(marker)
    rule = [segment for segment in segments if "─" in segment.text]
    assert rule
    for segment in rule:
        assert_status_style(segment)
    caption = next(segment for segment in segments if "run_1" in segment.text)
    assert_status_style(caption)
    facts = [
        segment
        for segment in segments
        if segment not in (marker, caption) and segment not in rule
    ]
    assert facts
    assert all(
        segment.style is not None and segment.style.color is None and segment.style.dim
        for segment in facts
    )


def test_chat_command_blocks_render_start_steer_and_stop_states() -> None:
    start = blocks.RunStartBlock.create("hello")
    start.update(_run_begin())
    start_text = _render_text(start.render())
    assert f"{rendering.ACCENT_CELL} hello" in start_text
    assert ">" not in start_text
    assert "run_1" not in start_text

    steer = blocks.RunSteerBlock.create(
        message="adjust",
        run_id="run_1",
    )
    steer_text = _render_text(steer.render())
    assert f"{rendering.ACCENT_CELL} adjust" in steer_text
    assert "+" not in steer_text
    assert "pending for next step" not in steer_text
    assert "run_1" not in steer_text
    assert not steer_text.splitlines()[0].strip()

    start_fragments = rendering.renderable_to_prompt_toolkit(start.render())
    steer_fragments = rendering.renderable_to_prompt_toolkit(steer.render())
    start_accent = next(
        fragment[0]
        for fragment in start_fragments
        if fragment[1] == rendering.ACCENT_CELL
        and f"bg:{rendering.START_CONTROL_ACCENT}" in fragment[0]
    )
    steer_accent = next(
        fragment[0]
        for fragment in steer_fragments
        if fragment[1] == rendering.ACCENT_CELL
        and f"bg:{rendering.STEER_CONTROL_ACCENT}" in fragment[0]
    )
    start_message = next(
        fragment[0] for fragment in start_fragments if "hello" in fragment[1]
    )
    steer_message = next(
        fragment[0] for fragment in steer_fragments if "adjust" in fragment[1]
    )

    assert start_accent == f"bg:{rendering.START_CONTROL_ACCENT}"
    assert steer_accent == f"bg:{rendering.STEER_CONTROL_ACCENT}"
    assert f"bg:{rendering.CONTROL_BAR_BACKGROUND}" in start_message
    assert f"bg:{rendering.CONTROL_BAR_BACKGROUND}" in steer_message

    steer.update(_model_step_begin(step_index=2))
    assert _render_text(steer.render()) == steer_text


def test_chat_prompt_uses_the_start_control_accent_without_a_prompt_marker() -> None:
    prompt = widgets.PromptBox(lambda _event: None, lambda: None)

    container = prompt.container()

    assert isinstance(container, VSplit)
    accent, content = container.children
    assert isinstance(accent, Window)
    assert accent.width == 1
    assert accent.style == "class:control.start"
    assert accent.char == rendering.ACCENT_CELL
    assert widgets._chat_ui_palette()["control.start"] == (
        f"bg:{rendering.START_CONTROL_ACCENT}"
    )
    assert rendering.CONTROL_BAR_BACKGROUND == rendering.INPUT_BACKGROUND
    assert isinstance(content, HSplit)
    input_row = content.children[1]
    assert isinstance(input_row, VSplit)
    padding = input_row.children[0]
    assert isinstance(padding, Window)
    assert padding.width == 1
    input_window = input_row.children[1]
    assert isinstance(input_window, Window)
    assert isinstance(input_window.content, BufferControl)
    assert input_window.content.input_processors is not None
    placeholder = input_window.content.input_processors[0]
    assert isinstance(placeholder, ConditionalProcessor)
    assert isinstance(placeholder.processor, AfterInput)
    assert placeholder.processor.style == "class:input.placeholder"
    assert placeholder.processor.text == "Ask anything"
    assert placeholder.filter()
    assert widgets._chat_ui_palette()["input.placeholder"] == (
        f"fg:#b8b8b8 bg:{rendering.INPUT_BACKGROUND}"
    )

    prompt.buffer.text = "hello"

    assert not placeholder.filter()


def test_chat_durable_response_wraps_markdown() -> None:
    long_text = " ".join(f"word{i}" for i in range(40))
    block = blocks.AssistantResponseBlock.from_parts(
        (TextPart(long_text),),
        max_width=44,
    )
    final_lines = _render_text(block.render(), width=80).splitlines()

    assert final_lines[0].startswith("• ")
    assert max(len(line) for line in final_lines) <= 44


@pytest.mark.parametrize("live", [False, True])
def test_chat_durable_response_matches_run_model_markdown(live: bool) -> None:
    markdown = "# Heading\n\nbefore\n\n---\n\n- item\n\nafter"
    durable = blocks.AssistantResponseBlock.from_parts((TextPart(markdown),))
    progress = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_1.0",
            (ProgressRow(markdown, "normal", format="markdown", prefix="• "),),
        ),
        live=live,
    )

    durable_text = _render_text(durable.render(), width=40).rstrip("\n")
    progress_text = _render_text(progress.render(), width=40).rstrip("\n")

    assert durable_text.startswith("• Heading\n")
    assert f"  {'─' * 38}\n" in durable_text
    assert durable_text == progress_text


def test_chat_live_viewport_keeps_latest_rows_and_reports_hidden_rows() -> None:
    renderables = [Text("\n".join(f"line {index}" for index in range(10)))]

    viewport = "".join(
        fragment[1]
        for fragment in rendering.renderables_to_prompt_toolkit(
            renderables,
            max_rows=4,
        )
    )

    assert rendering.renderables_height(renderables) == 10
    assert viewport.splitlines() == [
        "… 7 earlier live lines",
        "line 7",
        "line 8",
        "line 9",
    ]


def test_chat_progress_marker_style_does_not_leak_to_active_text() -> None:
    block = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_1.1",
            (ProgressRow("• thinking streaming hello", "active"),),
        )
    )

    segments = [
        segment
        for segment in rendering.render_segments(block.render(), width=60)
        if segment.text.strip()
    ]
    text = next(segment for segment in segments if "streaming hello" in segment.text)

    assert text.text.startswith("• ")
    assert text.style is None or text.style.color is None


def test_chat_slash_block_renders_command_usage_as_table_rows() -> None:
    block = blocks.SlashBlock(
        ":?",
        [
            "Chat Commands",
            "",
            ":help, :?                         Show help.",
            ":model, :models                  List or switch models.",
        ],
    )
    rendered = _render_text(block.render(), width=69)
    rendered_lines = rendered.splitlines()
    all_segments = rendering.render_segments(block.render(), width=80)
    segments = [segment for segment in all_segments if segment.text.strip()]

    assert "▌" not in rendered
    assert not rendered_lines[0].strip()
    assert rendered_lines[1].startswith(f"{rendering.ACCENT_CELL} :?")
    assert ">" not in rendered_lines[1]
    assert not rendered_lines[2].strip()
    assert ": Chat Commands" in rendered
    assert ":model, :models" in rendered
    assert "List or switch models." in rendered
    assert rendered.endswith("\n")
    command = next(segment for segment in segments if segment.text == ":model")
    argument = next(segment for segment in segments if segment.text == ":models")
    quick_accents = [
        segment
        for segment in all_segments
        if segment.text == rendering.ACCENT_CELL
        and segment.style is not None
        and segment.style.bgcolor is not None
        and segment.style.bgcolor.get_truecolor().hex
        == rendering.QUICK_COMMAND_CONTROL_ACCENT
    ]

    assert len(quick_accents) == 3
    assert all(
        segment.style is not None
        and segment.style.color is None
        and segment.style.bgcolor is not None
        and segment.style.bgcolor.get_truecolor().hex
        == rendering.QUICK_COMMAND_CONTROL_ACCENT
        for segment in quick_accents
    )
    assert rendering.QUICK_COMMAND_CONTROL_ACCENT not in {
        rendering.START_CONTROL_ACCENT,
        rendering.STEER_CONTROL_ACCENT,
    }
    assert command.style is not None
    assert command.style.color is not None
    assert argument.style is not None
    assert argument.style.color is not None
    assert all(segment.style is None or not segment.style.bold for segment in segments)


def test_chat_header_uses_wide_local_executor_layout() -> None:
    block = blocks.HeaderBlock(
        home="/tmp/toolang/agents/alice",
        version_label="0.1.0",
    )
    rendered = _render_text(block.render(), width=80)

    assert "█" not in rendered
    assert "⬤   ⬤" in rendered
    assert "Toolang" in rendered
    assert "0.1.0" in rendered
    assert "v0.1.0" not in rendered
    assert "model" not in rendered
    assert "home" in rendered
    assert "/tmp/toolang/agents/alice" in rendered
    assert "executor" in rendered
    assert "local" in rendered
    lines = rendered.splitlines()
    assert lines[0] == ""
    assert lines[1].startswith("╭")
    assert next(index for index, line in enumerate(lines) if "home" in line) < next(
        index for index, line in enumerate(lines) if "executor" in line
    )
    assert next(index for index, line in enumerate(lines) if "Toolang" in line) < next(
        index for index, line in enumerate(lines) if "⬤" in line
    )
    assert next(index for index, line in enumerate(lines) if "⬤" in line) == next(
        index for index, line in enumerate(lines) if "home" in line
    )
    bordered_lines = [line for line in lines if line]
    assert len({len(line) for line in bordered_lines}) == 1
    assert not bordered_lines[1].strip("│ ")
    assert "Toolang" in bordered_lines[2]
    assert "executor" in bordered_lines[-3]
    assert not bordered_lines[-2].strip("│ ")
    logo_line = next(line for line in lines if "Toolang" in line)
    assert logo_line.startswith("│" + " " * 23 + "Toolang")
    assert "Toolang 0.1.0" in logo_line
    assert "Toolang  0.1.0" not in logo_line


def test_chat_header_stacks_without_clipping_in_a_narrow_terminal() -> None:
    rendered = _render_text(
        blocks.HeaderBlock(
            home="/tmp/toolang/agents/alice-with-a-long-home",
            version_label="0.1.0",
        ).render(),
        width=40,
    )

    lines = rendered.splitlines()
    logo_index = next(index for index, line in enumerate(lines) if "⬤" in line)
    toolang_index = next(index for index, line in enumerate(lines) if "Toolang" in line)
    assert toolang_index > logo_index + 1
    assert all(len(line) <= 40 for line in lines)
    bordered_lines = [line for line in lines if line]
    assert len({len(line) for line in bordered_lines}) == 1
    unwrapped = rendered.replace("\n", "").replace("│", "").replace(" ", "")
    assert "alice-with-a-long-home" in unwrapped


def test_chat_header_keeps_logo_color_neutral_and_styles_metadata() -> None:
    segments = rendering.render_segments(
        blocks.HeaderBlock(
            home="/tmp/toolang/agents/alice",
            version_label="0.1.0",
        ).render(),
        width=80,
    )

    logo_blocks = [
        segment
        for segment in segments
        if segment.style is not None and segment.style.reverse
    ]
    logo_dots = [segment for segment in segments if "⬤" in segment.text]
    brand = next(segment for segment in segments if segment.text.strip() == "Toolang")
    version = next(segment for segment in segments if segment.text.strip() == "0.1.0")
    keys = [
        next(segment for segment in segments if segment.text.strip() == key)
        for key in ("home", "executor")
    ]
    values = [
        next(segment for segment in segments if segment.text.strip() == value)
        for value in ("/tmp/toolang/agents/alice", "local")
    ]

    assert "█" not in "".join(segment.text for segment in segments)
    assert sum(len(segment.text) for segment in logo_blocks) == 16
    assert all(segment.text.isspace() for segment in logo_blocks)
    assert all(
        segment.style is not None
        and segment.style.color is None
        and segment.style.bgcolor is None
        for segment in logo_blocks
    )
    assert logo_dots
    assert all(
        segment.style is None
        or (
            segment.style.color is None
            and segment.style.bgcolor is None
            and not segment.style.reverse
        )
        for segment in logo_dots
    )
    assert brand.style is not None and brand.style.bold
    assert brand.style.color is not None
    assert brand.style.color.name == "bright_cyan"
    assert version.style is None or not version.style.dim
    assert all(segment.style is not None and segment.style.dim for segment in keys)
    assert all(
        segment.style is None or (not segment.style.bold and not segment.style.dim)
        for segment in values
    )


def test_chat_model_label_uses_default_or_selected_model() -> None:
    payload = {
        "default": "openai/gpt-5[openai]",
        "items": [
            {
                "selector": "openai/gpt-5[openai]",
                "ref": "openai/gpt-5",
                "provider": "openai",
                "model": "gpt-5",
            },
            {
                "selector": "openai/o3[openai]",
                "ref": "openai/o3",
                "provider": "openai",
                "model": "o3",
            },
        ],
    }

    assert slashes.chat_model_label(payload, {}) == "openai/gpt-5"
    assert (
        slashes.chat_model_label(payload, {"model": "openai/o3[openai]"}) == "openai/o3"
    )


def test_chat_model_list_lines_render_as_columns() -> None:
    payload = {
        "default": "deepseek/deepseek-v4-flash[deepseek]",
        "items": [
            {
                "selector": "deepseek/deepseek-v4-flash[deepseek]",
                "provider": "deepseek",
                "adapter": "chat_completions",
            },
            {
                "selector": "deepseek/deepseek-v4-pro[deepseek]",
                "provider": "deepseek",
                "adapter": "chat_completions",
            },
        ],
    }
    block = blocks.SlashBlock(
        ":model", ["Available Models", *slashes._chat_model_list_lines(payload)]
    )
    rendered = _render_text(block.render(), width=120)
    model_lines = [line for line in rendered.splitlines() if "deepseek/" in line]
    rendered_lines = rendered.splitlines()

    assert ": Available Models" in rendered
    assert not rendered_lines[rendered_lines.index(": Available Models") + 1].strip()
    assert "deepseek/deepseek-v4-flash[deepseek]" in rendered
    assert "default" in rendered
    assert len(model_lines) == 2
    assert model_lines[0].index("deepseek  chat_completions") == model_lines[1].index(
        "deepseek  chat_completions"
    )


def test_chat_status_bar_right_aligns_the_model_without_hotkeys(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(widgets.StatusBar, "_terminal_width", staticmethod(lambda: 80))
    text = "".join(
        fragment
        for _style, fragment in widgets.StatusBar(
            "agic:chat", "runtime model"
        )._render()
    )

    assert "^d exit" not in text
    assert "↑↓ history" not in text
    assert text.startswith("■ agic:chat")
    assert text.endswith("runtime model")
    assert get_cwidth(text) == 80


def test_chat_status_bar_animates_its_marker_and_shows_elapsed_time() -> None:
    status = widgets.StatusBar("agic:chat", "runtime model")
    idle = status._render()
    idle_text = "".join(fragment for _style, fragment in idle)

    status.set_running(True)
    running = status._render()
    running_text = "".join(fragment for _style, fragment in running)
    status.set_activity(2, 68)
    next_frame = status._render()

    assert idle_text.startswith("■ agic:chat")
    assert idle_text.endswith("runtime model")
    assert running_text.startswith("◧ agic:chat · 0s")
    assert running_text.endswith("runtime model")
    assert idle[:3] == [
        ("class:status.marker", "■"),
        ("class:status", " "),
        ("class:status", "agic:chat"),
    ]
    assert running[:4] == [
        ("class:status.marker", "◧"),
        ("class:status", " "),
        ("class:status", "agic:chat"),
        ("class:status.elapsed", " · 0s"),
    ]
    assert next_frame[:4] == [
        ("class:status.marker", "◨"),
        ("class:status", " "),
        ("class:status", "agic:chat"),
        ("class:status.elapsed", " · 1m 08s"),
    ]
    assert next_frame[-1] == ("class:status", "runtime model")
    assert status.spinner_index == 2
    assert status.elapsed_seconds == 68

    status.set_running(False)
    assert status._render() == idle


def test_chat_status_bar_keeps_the_default_model_at_the_right_edge(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(widgets.StatusBar, "_terminal_width", staticmethod(lambda: 80))
    status = widgets.StatusBar("flow:research", "openai/gpt-5")
    idle = "".join(text for _style, text in status._render())

    status.set_active_runnable("agic:chat")
    status.set_running(True)
    status.set_activity(0, 18)
    running = "".join(text for _style, text in status._render())

    assert idle.startswith("■ flow:research")
    assert running.startswith("◧ agic:chat · 18s")
    assert running.endswith("flow:research · openai/gpt-5")
    assert idle.rindex("openai/gpt-5") == running.rindex("openai/gpt-5")
    assert get_cwidth(idle) == get_cwidth(running) == 80


def test_chat_status_bar_omits_the_matching_default_runnable() -> None:
    status = widgets.StatusBar("agic:chat", "openai/gpt-5")
    status.set_active_runnable("agic:chat")
    status.set_running(True)

    text = "".join(fragment for _style, fragment in status._render())

    assert text.count("agic:chat") == 1
    assert text.endswith("openai/gpt-5")


def test_chat_status_bar_truncates_labels_without_moving_the_model_edge(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(widgets.StatusBar, "_terminal_width", staticmethod(lambda: 40))
    status = widgets.StatusBar(
        "flow:a_very_long_default_runnable",
        "openai/gpt-5",
    )
    status.set_active_runnable("agic:a_very_long_active_runnable")
    status.set_running(True)
    status.set_activity(0, 18)

    text = "".join(fragment for _style, fragment in status._render())

    assert get_cwidth(text) == 40
    assert text.endswith("openai/gpt-5")
    assert "· openai/gpt-5" in text


def test_chat_status_palette_uses_input_background_for_marker_color() -> None:
    palette = widgets._chat_ui_palette()

    assert palette["status"] == ""
    assert palette["status.marker"] == f"fg:{rendering.INPUT_BACKGROUND}"
    assert palette["status.elapsed"] == "dim"
    assert (
        not {
            "status.text",
            "status.agic",
            "status.flow",
            "status.model",
        }
        & palette.keys()
    )


def test_chat_status_spinner_styles_use_single_width_frames() -> None:
    step = tui._STATUS_SPINNER_FRAME_DURATION

    assert step == pytest.approx(0.3)
    assert tui._STATUS_ACTIVITY_TICK == pytest.approx(0.3)
    assert widgets._STATUS_SPINNER_STYLE == "squares"
    assert widgets._STATUS_SPINNER_STYLES == {
        "quadrants": (" ", ("▖", "▘", "▝", "▗")),
        "hatch": ("▦", ("▤", "▥", "▧", "▨")),
        "dots": ("⠿", ("⠾", "⠷", "⠟", "⠻")),
        "triangles": ("▪︎", ("◤", "◥", "◢", "◣")),
        "squares": ("■", ("◧", "◩", "◨", "◪")),
    }
    assert widgets._STATUS_IDLE_MARKER == "■"
    assert widgets._STATUS_SPINNER_FRAMES == ("◧", "◩", "◨", "◪")
    assert widgets._STATUS_IDLE_MARKER not in widgets._STATUS_SPINNER_FRAMES
    assert all(
        get_cwidth(character) == 1
        for idle, frames in widgets._STATUS_SPINNER_STYLES.values()
        for character in (idle, *frames)
    )
    assert tui._status_spinner_index(0) == 0
    assert tui._status_spinner_index(step) == 1
    assert tui._status_spinner_index(step * 3) == 3
    assert tui._status_spinner_index(step * 4 + 1e-9) == 0


def test_chat_status_qualifies_resolved_runnables() -> None:
    payload = {
        "items": [
            {"kind": "agic", "name": "chat"},
            {"kind": "flow", "name": "research"},
        ]
    }

    assert tui._qualified_runnable_label("agic:chat", payload) == "agic:chat"
    assert tui._qualified_runnable_label("flow:research", payload) == "flow:research"
    assert tui._qualified_runnable_label("research", payload) == "flow:research"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (59, "59s"), (60, "1m 00s"), (68, "1m 08s"), (3661, "1h 01m 01s")],
)
def test_chat_status_elapsed_time_uses_whole_seconds(
    seconds: int, expected: str
) -> None:
    assert widgets._format_elapsed_seconds(seconds) == expected


def test_chat_tui_floors_and_freezes_status_elapsed_time() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        selects={},
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.status_bar.set_running(True)
    app._status_activity_started_at = 100.0

    app._update_status_activity(168.9)

    assert app.status_bar.elapsed_seconds == 68
    assert "1m 08s" in "".join(text for _style, text in app.status_bar._render())

    app._status_completed_elapsed_seconds = 68
    app._update_status_activity(171.2)

    assert app.status_bar.elapsed_seconds == 68


def test_chat_tui_animates_status_only_while_a_run_is_active(
    monkeypatch: Any,
) -> None:
    async def exercise() -> None:
        app = tui.ChatTuiApp(
            thread_id=None,
            selects={},
            home="/tmp/agent",
            input_history=None,
            client=FakeClient(),
        )
        app.loop = asyncio.get_running_loop()
        activity_updated = asyncio.Event()
        update_status_activity = app._update_status_activity

        def record_status_activity(now: float) -> None:
            update_status_activity(now)
            if app.status_bar.spinner_index > 0:
                activity_updated.set()

        monkeypatch.setattr(app, "_update_status_activity", record_status_activity)
        animation = asyncio.create_task(app._animate_status())
        try:
            app._set_status_running(True)
            await asyncio.wait_for(activity_updated.wait(), timeout=0.5)

            app._set_status_running(False)

            assert app.status_bar.spinner_index == 0
            assert app.status_bar.elapsed_seconds == 0
            assert not app.status_bar.running
        finally:
            animation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await animation

    monkeypatch.setattr(tui, "_STATUS_ACTIVITY_TICK", 0.001)
    monkeypatch.setattr(tui, "_STATUS_SPINNER_FRAME_DURATION", 0.001)
    monkeypatch.setattr(tui, "_MIN_STATUS_ACTIVITY_DURATION", 0.0)
    monkeypatch.setattr(tui, "_status_spinner_index", lambda elapsed: 1)
    asyncio.run(exercise())


def test_chat_tui_keeps_short_run_activity_visible(monkeypatch: Any) -> None:
    async def exercise() -> None:
        app = tui.ChatTuiApp(
            thread_id=None,
            selects={},
            home="/tmp/agent",
            input_history=None,
            client=FakeClient(),
        )
        app.loop = asyncio.get_running_loop()
        activity_stopped = asyncio.Event()
        stop_status_activity = app._stop_status_activity

        def record_activity_stop() -> None:
            stop_status_activity()
            activity_stopped.set()

        monkeypatch.setattr(app, "_stop_status_activity", record_activity_stop)
        animation = asyncio.create_task(app._animate_status())
        try:
            app.status_bar.set_active_runnable("agic:active")
            app._set_status_running(True)
            app._set_status_running(False)

            assert app.status_bar.running
            assert app.status_bar.active_runnable_label == "agic:active"
            assert app.status_bar._render()[0] == (
                "class:status.marker",
                "◧",
            )
            await asyncio.wait_for(activity_stopped.wait(), timeout=0.5)
            assert not app.status_bar.running
            assert app.status_bar.active_runnable_label is None
            assert app.status_bar._render()[0] == (
                "class:status.marker",
                "■",
            )
        finally:
            animation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await animation

    monkeypatch.setattr(tui, "_MIN_STATUS_ACTIVITY_DURATION", 0.01)
    monkeypatch.setattr(tui, "_STATUS_ACTIVITY_TICK", 0.001)
    monkeypatch.setattr(tui, "_STATUS_SPINNER_FRAME_DURATION", 0.001)
    asyncio.run(exercise())


def test_chat_tui_new_run_cancels_pending_activity_stop(monkeypatch: Any) -> None:
    async def exercise() -> None:
        app = tui.ChatTuiApp(
            thread_id=None,
            selects={},
            home="/tmp/agent",
            input_history=None,
            client=FakeClient(),
        )
        app.loop = asyncio.get_running_loop()

        app._set_status_running(True)
        app._set_status_running(False)
        pending_stop = app._status_stop_handle

        assert pending_stop is not None

        app._set_status_running(True)

        assert pending_stop.cancelled()
        assert app._status_stop_handle is None
        assert app.status_bar.running
        app._stop_status_activity()

    monkeypatch.setattr(tui, "_MIN_STATUS_ACTIVITY_DURATION", 0.01)
    asyncio.run(exercise())


def test_chat_tui_run_lifecycle_starts_and_stops_status_activity() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        selects={},
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )

    app.start_run(QueuedCall("hello", {}))

    assert app.run_in_flight.is_set()
    assert app.status_bar.running

    app._finish_active_run()

    assert not app.run_in_flight.is_set()
    assert not app.status_bar.running


def test_chat_status_bar_error_uses_full_width_error_line(monkeypatch: Any) -> None:
    monkeypatch.setattr(widgets.StatusBar, "_terminal_width", staticmethod(lambda: 40))
    status = widgets.StatusBar("agic:chat", "runtime model")
    status.set_error("No active run to steer.")

    rendered = status._render()
    text = "".join(fragment for _style, fragment in rendered)

    assert rendered == [("class:status.error", text)]
    assert text.startswith("! No active run to steer.")
    assert len(text) == 40


def test_chat_tui_uses_truecolor_for_live_block_rendering() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        selects={},
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )

    assert app.app.color_depth == ColorDepth.DEPTH_24_BIT


def test_chat_tui_keeps_default_model_and_clears_status_error() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        selects={},
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )

    assert app.status_bar.model_label == "openai/gpt-5"
    assert app.status_bar.runnable_label == "agic:chat"
    app.handle_run_event(_model_step_begin(model="deepseek/deepseek-chat"))
    assert app.status_bar.model_label == "openai/gpt-5"

    app.status_bar.set_error("Model selector matched no models")
    assert app.status_bar.error_message

    app.prompt.buffer.text = "retry"

    assert app.status_bar.error_message == ""


def test_chat_tui_status_bar_compacts_the_current_default_runnable() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        selects={"agic": "default"},
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )

    assert app.status_bar.runnable_label == "agic:chat"

    app.selects = {"agic": "review"}
    assert app._runnable_label() == "agic:review"

    app.selects = {"flow": "research"}
    assert app._runnable_label() == "flow:research"


def test_chat_tui_uses_the_root_run_runnable_as_active_status(
    monkeypatch: Any,
) -> None:
    app = tui.ChatTuiApp(
        thread_id="term_status",
        selects={"flow": "research"},
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    monkeypatch.setattr(tui.events, "handle_run_event", lambda _event, _app: None)
    app.status_bar.set_active_runnable("flow:research")
    app.status_bar.set_running(True)

    app.handle_run_event(_run_begin(executable_name="review"))
    app.handle_run_event(
        _run_begin(
            run_id="run_child",
            parent_run_id="run_1",
            executable_kind="flow",
            executable_name="child",
        )
    )

    assert app.status_bar.active_runnable_label == "agic:review"


def test_chat_tui_applies_default_settings_while_a_run_is_active() -> None:
    class SettingsClient(FakeClient):
        def apply_settings(
            self,
            commands: tuple[RunOverride, ...],
            selects: Mapping[str, object],
        ) -> Mapping[str, object]:
            return apply_session_commands(selects, commands)

    app = tui.ChatTuiApp(
        thread_id="term_status",
        selects={},
        home="/tmp/agent",
        input_history=None,
        client=SettingsClient(),
    )
    app.active_run_id = "run_active"
    app.status_bar.set_active_runnable("agic:chat")
    app.status_bar.set_running(True)

    app.handle_submit(":flow research")

    runnable_changed = "".join(text for _style, text in app.status_bar._render())
    assert app.status_bar.active_runnable_label == "agic:chat"
    assert app.status_bar.runnable_label == "flow:research"
    assert runnable_changed.endswith("flow:research · openai/gpt-5")
    assert app.queue == []

    app.handle_submit(":model custom/model")

    model_changed = "".join(text for _style, text in app.status_bar._render())
    assert app.status_bar.active_runnable_label == "agic:chat"
    assert app.status_bar.model_label == "custom/model"
    assert model_changed.endswith("flow:research · custom/model")

    app.handle_submit(":agic chat")

    restored = "".join(text for _style, text in app.status_bar._render())
    assert restored.count("agic:chat") == 1
    assert restored.endswith("custom/model")


def test_chat_default_settings_clear_explicit_model_and_runnable() -> None:
    selects = {
        "model": "openai/o3[openai]",
        "agic": "review",
    }

    updated = apply_session_commands(
        selects,
        (
            RunOverride("default", "model", None),
            RunOverride("default", "runnable", None),
        ),
    )

    assert updated == {}


def test_chat_tui_creates_a_thread_only_for_the_first_submission(
    monkeypatch: Any,
) -> None:
    started = threading.Event()

    class LazyClient(FakeClient):
        def __init__(self) -> None:
            self.created = 0

        def create_thread(self) -> str:
            self.created += 1
            return "term_lazy"

        def start_run(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            started.set()

    client = LazyClient()
    app = tui.ChatTuiApp(
        thread_id=None,
        selects={},
        home="/tmp/agent",
        input_history=None,
        client=client,
    )
    monkeypatch.setattr(tui.rendering, "write_renderable", lambda *_args: None)

    app.handle_submit(":help")
    assert client.created == 0

    app.handle_submit("hello")
    assert started.wait(timeout=1)
    assert client.created == 1
    assert app.thread_id == "term_lazy"


def test_chat_thread_creation_error_is_a_submission_error_in_scrollback(
    monkeypatch: Any,
) -> None:
    class FailingClient(FakeClient):
        def create_thread(self) -> str:
            raise ValueError("thread creation failed")

    app = tui.ChatTuiApp(
        thread_id=None,
        selects={},
        home="/tmp/agent",
        input_history=None,
        client=FailingClient(),
    )
    rendered: list[str] = []
    monkeypatch.setattr(
        tui.rendering,
        "write_renderable",
        lambda value: rendered.append(_render_text(value)),
    )

    app.start_run(QueuedCall("hello", {}))

    output = "\n".join(rendered)
    assert f"{rendering.ACCENT_CELL} hello" in output
    assert ">" not in output
    assert "• thread creation failed" in output
    assert "run failed" not in output
    assert app.status_bar.error_message == ""
    assert not app.status_bar.running
    assert not app.run_in_flight.is_set()


def test_chat_queue_captures_settings_at_submission_time() -> None:
    class SettingsClient(FakeClient):
        def apply_settings(
            self,
            commands: tuple[RunOverride, ...],
            selects: Mapping[str, object],
        ) -> Mapping[str, object]:
            result = dict(selects)
            result["model"] = commands[0].value
            return result

    app = tui.ChatTuiApp(
        thread_id="term_busy",
        selects={},
        home="/tmp/agent",
        input_history=None,
        client=SettingsClient(),
    )
    app.active_run_id = "run_busy"

    app.handle_submit(":model first")
    app.handle_submit("first call")
    app.handle_submit(":model second")
    app.handle_submit("second call")

    assert [item.source for item in app.queue] == ["first call", "second call"]
    assert [item.selects["model"] for item in app.queue] == ["first", "second"]


def test_chat_tui_uses_queued_runnable_snapshot_for_the_next_active_status() -> None:
    app = tui.ChatTuiApp(
        thread_id="term_busy",
        selects={"agic": "chat"},
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.active_run_id = "run_busy"
    app.run_in_flight.set()
    app.status_bar.set_active_runnable("agic:chat")
    app.status_bar.set_running(True)
    app.queue.append(QueuedCall("queued", {"flow": "research"}))

    app._finish_active_run()

    assert app.status_bar.running
    assert app.status_bar.active_runnable_label == "flow:research"
    assert app.run_in_flight.is_set()


def test_chat_tui_empty_input_requires_two_interrupts_to_exit() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        selects={},
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )

    assert app.handle_ui_event(ChatUIEvent("interrupt")) is False
    assert app.interrupt_exit_pending
    assert app.status_bar.error_message == "Press Ctrl-C again to exit."

    assert app.handle_ui_event(ChatUIEvent("interrupt")) is True


def test_chat_tui_typing_resets_pending_interrupt_exit() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        selects={},
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )

    assert app.handle_ui_event(ChatUIEvent("interrupt")) is False
    app.prompt.buffer.text = "hello"

    assert not app.interrupt_exit_pending
    assert app.status_bar.error_message == ""


def test_chat_tui_removes_live_block_before_writing_scrollback(
    monkeypatch: Any,
) -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        selects={},
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    block = blocks.RunStopBlock.create(_run_begin())
    block.update(_run_end(status="canceled"))
    app.unfinalized_blocks.append(block)

    def write_renderable(renderable: RenderableType | None, **kwargs: object) -> None:
        del renderable, kwargs
        assert block not in app.unfinalized_blocks

    monkeypatch.setattr(tui.rendering, "write_renderable", write_renderable)

    tui.ChatTuiAppContext(app).finalize_block(block)


def test_chat_tui_commits_live_finalization_in_one_terminal_transaction(
    monkeypatch: Any,
) -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        selects={},
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    block = blocks.ExecutionProgressBlock(
        ProgressBlock("step:run_1.1", (ProgressRow("• completed"),))
    )
    app.unfinalized_blocks.append(block)
    order: list[str] = []
    monkeypatch.setattr(
        type(app.app),
        "is_running",
        property(lambda _application: True),
    )
    monkeypatch.setattr(
        app.app.renderer,
        "erase",
        lambda *, leave_alternate_screen: order.append(
            f"erase:{leave_alternate_screen}"
        ),
    )

    def write_scrollback(renderables: list[RenderableType | None]) -> None:
        assert block not in app.unfinalized_blocks
        assert len(renderables) == 1
        order.append("write")

    monkeypatch.setattr(app, "_write_scrollback", write_scrollback)
    monkeypatch.setattr(app.app, "invalidate", lambda: order.append("invalidate"))

    tui.ChatTuiAppContext(app).finalize_block(block)
    assert order == []

    app._commit_ui_update()

    assert order == ["erase:False", "write", "invalidate"]
    assert app._pending_scrollback == []
    assert block not in app.unfinalized_blocks


def test_chat_tui_replaces_failed_model_live_state_in_scrollback_transaction(
    monkeypatch: Any,
) -> None:
    app = tui.ChatTuiApp(
        thread_id="thread_1",
        selects={},
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    monkeypatch.setattr(
        type(app.app),
        "is_running",
        property(lambda _application: True),
    )
    writes: list[str] = []
    erases: list[bool] = []

    def write_scrollback(renderables: list[RenderableType | None]) -> None:
        writes.append("".join(_render_text(item) for item in renderables))

    monkeypatch.setattr(app, "_write_scrollback", write_scrollback)
    monkeypatch.setattr(
        app.app.renderer,
        "erase",
        lambda *, leave_alternate_screen: erases.append(leave_alternate_screen),
    )

    app.handle_run_event(_run_begin())
    app._commit_ui_update()

    app.handle_run_event(_model_step_begin(model="openai/gpt-5"))
    assert "thinking" in "".join(
        _render_text(block.render()) for block in app.unfinalized_blocks
    )
    app._commit_ui_update()

    app.handle_run_event(
        StepEnd(
            step=StepPath.parse("run_1.1"),
            kind="model",
            status="failed",
            error="You have no credits remaining.",
            finished_at="2026-01-01T00:00:02Z",
        )
    )
    app._commit_ui_update()

    assert erases == [False]
    assert len(writes) == 1
    assert "thinking" not in writes[0]
    assert "You have no credits remaining." in writes[0]
    assert all(
        not isinstance(block, blocks.ExecutionProgressBlock)
        for block in app.unfinalized_blocks
    )


def test_chat_tui_show_command_renders_durable_markdown(
    monkeypatch: Any,
) -> None:
    app = tui.ChatTuiApp(
        thread_id="thread_1",
        selects={},
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    written: list[RenderableType | None] = []
    monkeypatch.setattr(
        tui.rendering,
        "write_renderables",
        lambda renderables: written.extend(renderables),
    )

    app.handle_submit(":show run_saved")

    assert len(written) == 1
    rendered = _render_text(written[0])
    result_lines = [line.rstrip() for line in rendered.splitlines()]
    divider = "• run_saved result "
    divider += "─" * (42 - len(divider))
    divider_index = result_lines.index(divider)
    assert result_lines[divider_index : divider_index + 3] == [
        divider,
        "",
        "• durable result",
    ]
    assert rendered.endswith("\n\n")

    segments = [
        segment
        for segment in rendering.render_segments(written[0], width=80)
        if segment.text.strip()
    ]
    marker = next(segment for segment in segments if segment.text == "•")
    caption = next(
        segment for segment in segments if "run_saved result" in segment.text
    )
    rule = [segment for segment in segments if "─" in segment.text]
    response = next(segment for segment in segments if "durable result" in segment.text)
    assert marker.style is not None and marker.style.dim
    assert caption.style is not None and caption.style.dim
    assert rule and all(
        segment.style is not None and segment.style.dim for segment in rule
    )
    assert response.style is None or not response.style.dim


def test_chat_show_result_divider_truncates_without_wrapping(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(rendering, "terminal_width", lambda: 24)
    block = blocks.SlashResultBlock(
        message=":show run_saved_with_a_long_identifier",
        run_id="run_saved_with_a_long_identifier",
        parts=(TextPart("durable result"),),
        max_width=24,
    )

    lines = [
        line.rstrip() for line in _render_text(block.render(), width=24).splitlines()
    ]
    dividers = [line for line in lines if line.startswith("•") and line.endswith("─")]

    assert len(dividers) == 1
    assert len(dividers[0]) == 24
    assert dividers[0].endswith("─")


def _render_text(renderable: RenderableType | None, *, width: int = 80) -> str:
    return "".join(
        segment.text
        for segment in rendering.render_segments(renderable, width=width)
        if not segment.control
    )


def _run_begin(
    *,
    run_id: str = "run_1",
    parent_run_id: str | None = None,
    executable_kind: str = "agic",
    executable_name: str = "test",
) -> RunBegin:
    return RunBegin(
        run=run_id,
        control=ControlRef(run_id, 0),
        parent=(
            StepPath.parse(f"{parent_run_id}.2") if parent_run_id is not None else None
        ),
        started_at="2026-01-01T00:00:00Z",
        runnable=f"{executable_kind}:{executable_name}",
    )


def _run_end(
    *,
    run_id: str = "run_1",
    status: Literal["running", "succeeded", "failed", "canceled"],
    output_step_index: int = 1,
) -> RunEnd:
    return RunEnd(
        run=run_id,
        status=status,
        output=(
            _output(StepPath.parse(f"{run_id}.{output_step_index}"))
            if run_id == "run_1" and status == "succeeded"
            else None
        ),
        finished_at="2026-01-01T00:00:03Z",
    )


def _model_step_begin(
    *,
    run_id: str = "run_1",
    step_index: int = 1,
    model: str | None = None,
) -> StepBegin:
    return StepBegin(
        step=StepPath.parse(f"{run_id}.{step_index}"),
        kind="model",
        input=(),
        given=_model_given(model or "test/model"),
        started_at="2026-01-01T00:00:01Z",
    )


def _tool_step_begin(*, step_index: int = 1) -> StepBegin:
    return StepBegin(
        step=StepPath.parse(f"run_1.{step_index}"),
        kind="tool",
        input=(),
        given=_tool_given(),
        started_at="2026-01-01T00:00:01Z",
    )


def _model_step_end(
    *,
    run_id: str = "run_1",
    output: str,
    step_index: int = 1,
    finished_at: str = "2026-01-01T00:00:02Z",
) -> StepEnd:
    return StepEnd(
        step=StepPath.parse(f"{run_id}.{step_index}"),
        kind="model",
        status="succeeded",
        output=_parts(TextPart(text=output)),
        noted=ModelStepNoted(tokens=ModelTokenCount(input=1, output=1)),
        finished_at=finished_at,
    )


def _flow_step_begin(*, step_index: int = 1) -> StepBegin:
    return StepBegin(
        step=StepPath.parse(f"run_1.{step_index}"),
        kind="par",
        input=(),
        started_at="2026-01-01T00:00:01Z",
        given=MapStmt(span=Span(line=1), runnable="summarize", lanes=2),
    )


def _flow_step_end(*, step_index: int = 1) -> StepEnd:
    return StepEnd(
        step=StepPath.parse(f"run_1.{step_index}"),
        kind="par",
        status="succeeded",
        output=Local.typed("Json[]", (), "_", 1),
        finished_at="2026-01-01T00:00:02Z",
    )


def _child_run_step_begin(
    *, step_index: int = 2, step: StepPath | str | None = None
) -> StepBegin:
    return StepBegin(
        step=StepPath.parse(step or f"run_1.{step_index}"),
        kind="run",
        input=(),
        started_at="2026-01-01T00:00:01Z",
        given=RunStmt(span=Span(line=1), runnable="summarize"),
    )


def _child_run_step_end(
    *, step_index: int = 2, step: StepPath | str | None = None
) -> StepEnd:
    return StepEnd(
        step=StepPath.parse(step or f"run_1.{step_index}"),
        kind="run",
        status="succeeded",
        output=_parts(TextPart(text="done")),
        finished_at="2026-01-01T00:00:02Z",
    )


def _tool_step_end(
    *,
    run_id: str = "run_1",
    step_index: int = 1,
    finished_at: str = "2026-01-01T00:00:02Z",
) -> StepEnd:
    return StepEnd(
        step=StepPath.parse(f"{run_id}.{step_index}"),
        kind="tool",
        status="succeeded",
        output=_parts(
            ToolCallPart(
                tool_call_id="call_1",
                tool_name="shell__execute",
                tool_family="shell",
                input={"command": "echo ok"},
            ),
            ToolResultPart(
                tool_call_id="call_1",
                tool_name="shell__execute",
                tool_family="shell",
                output={"stdout": "ok\n"},
            ),
        ),
        finished_at=finished_at,
    )


@dataclass
class FakeApp:
    live_blocks: list[blocks.MutableBlock] = field(default_factory=list)
    finalized: list[blocks.MutableBlock] = field(default_factory=list)
    active_run: str | None = None
    finished: bool = False
    presenter: ChatRunPresenter = field(default_factory=ChatRunPresenter)

    def get_selects(self) -> dict[str, object]:
        return {}

    def get_client(self) -> Any:
        raise NotImplementedError

    def get_queue(self) -> list[QueuedCall]:
        return []

    def get_active_run(self) -> str | None:
        return self.active_run

    def get_thread_id(self) -> str | None:
        return "thread_1"

    def set_active_run(self, run_id: str | None) -> None:
        self.active_run = run_id

    def get_live_blocks(self) -> list[blocks.MutableBlock]:
        return self.live_blocks

    def get_presenter(self) -> ChatRunPresenter:
        return self.presenter

    def ensure_thread_id(self) -> str:
        return "thread_1"

    def is_busy(self) -> bool:
        return self.active_run is not None

    def finalize_block(self, block: blocks.MutableBlock) -> None:
        if block in self.live_blocks:
            self.live_blocks.remove(block)
        self.finalized.append(block)

    def finish_run(self) -> None:
        self.active_run = None
        self.finished = True

    def set_status_error(self, message: str) -> None:
        del message

    def refresh_status(self) -> None:
        pass

    def replace_input(self, text: str) -> None:
        del text

    def request_steer(self, message: str) -> None:
        del message

    def request_exit(self) -> None:
        pass


class FakeClient(ChatClient):
    def list_models(self) -> dict[str, object]:
        return {
            "default": "openai/gpt-5[openai]",
            "items": [
                {
                    "selector": "openai/gpt-5[openai]",
                    "ref": "openai/gpt-5",
                    "provider": "openai",
                    "model": "gpt-5",
                }
            ],
        }

    def list_executables(self, kind: str) -> dict[str, object]:
        if kind == "runnable":
            return {
                "default": "agic:chat",
                "items": [
                    {"kind": "agic", "name": "chat"},
                    {"kind": "flow", "name": "research"},
                ],
            }
        return {"items": []}

    def create_thread(self) -> str:
        return "thread_1"

    def apply_settings(
        self,
        commands: tuple[RunOverride, ...],
        selects: Mapping[str, object],
    ) -> Mapping[str, object]:
        del commands
        return dict(selects)

    def get_result(
        self,
        run_id: str | None,
        *,
        thread_id: str | None,
    ) -> ChatResult:
        del thread_id
        return ChatResult(
            run_id=run_id or "run_latest",
            output=(TextPart("durable result"),),
        )

    def start_run(
        self,
        thread_id: str,
        message: str,
        selects: Mapping[str, object],
        on_event: Callable[[RunEvent], None],
        on_error: Callable[[str], None],
    ) -> None:
        del thread_id, message, selects, on_event, on_error

    def stop_run(
        self,
        run_id: str,
        on_error: Callable[[str], None],
    ) -> None:
        del run_id, on_error

    def steer_run(
        self,
        run_id: str,
        message: str,
        on_error: Callable[[str], None],
    ) -> None:
        del run_id, message, on_error
