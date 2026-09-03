from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from io import StringIO
import threading
from types import SimpleNamespace
from typing import Any, Literal, cast

from prompt_toolkit.application.current import create_app_session, set_app
from prompt_toolkit.completion import CompleteEvent, Completion
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.input import DummyInput
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.layout import ConditionalContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.processors import AfterInput, ConditionalProcessor
from prompt_toolkit.layout.screen import Screen
from prompt_toolkit.keys import Keys
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth
from rich.color import Color, ColorType
from rich.console import RenderableType
from rich.segment import Segment
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
from toolang.common.errors import ToolangError
from toolang.cli.toolang.commands.chat import (
    blocks,
    completion,
    events,
    rendering,
    shortcuts,
    slashes,
    tui,
    widgets,
)
from toolang.cli.toolang.commands.chat.policy import update_session_setting
from toolang.cli.toolang.commands.chat.input import QuickCommand
from toolang.cli.toolang.commands.chat.events import ChatUIEvent
from toolang.cli.toolang.commands.chat.base import (
    ChatClient,
    ChatExecutorMetadata,
    ChatResult,
    ChatRunState,
    QueuedCall,
    RunAccepted,
    RunBlocked,
    RunDisconnected,
    RunRecovered,
)
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
from toolang.base.types.model import (
    ModelParameters,
    ModelRequest,
    ReasoningParameters,
)
from toolang.base.types.policy import RunPolicy
from toolang.execution.schemas import (
    RunControlRefData,
    RunDetail,
    RunRequest,
    RunnableRequest,
)
from toolang.execution.types import (
    ControlRef,
    Local,
    ModelAccounting,
    ModelCost,
    ModelCostLine,
    ModelOverride,
    ModelStepGiven,
    ModelStepNoted,
    ModelTokenCount,
    ModelUsageMeter,
    Occurrence,
    OccurrencePosition,
    RunOverride,
    SessionSetting,
    StepPath,
    Pointer,
    ToolStepGiven,
)
from toolang.lang.ast import MapStmt, RunStmt, Span
from toolang.lang.input import RunnableInputRaw
from tests.support import chat_tui_pty


_CONTAINER_ID = "176191c1528b8e2861cc16422dee13ade59d4977c2148a9ebf5d36a06f090abb"
_HOST_DESCRIPTION = "macOS 27.0 arm64"
_HOST_SANDBOX_VALUE = f"host · {_HOST_DESCRIPTION}"


class _TerminalOutput(DummyOutput):
    columns = 100

    def get_size(self) -> Size:
        return Size(rows=30, columns=self.columns)


def _render_chat_layout(app: tui.ChatTuiApp) -> Screen:
    # Match Application._redraw: controls cache fragments for each render counter.
    app.app.render_counter += 1
    app.app.renderer.render(app.app, app.app.layout)
    screen = app.app.renderer.last_rendered_screen
    assert screen is not None
    return screen


def _parts(*parts: Part) -> Local:
    return Local.typed("Part[]", tuple(parts), "_", 0)


def _output(step: StepPath) -> Local:
    return Local.typed("Part[]", Pointer.step(step, "output", "value"), "_", 0)


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
    app.live_blocks.append(blocks.RunControlBlock.create("hello"))

    assert [block.type for block in app.live_blocks] == ["RunControlBlock"]
    assert "hello" in _render_text(app.live_blocks[0].render())

    events.handle_run_event(_run_begin(), app)

    assert [block.type for block in app.live_blocks] == ["RunSummaryBlock"]
    assert [block.type for block in app.finalized] == ["RunControlBlock"]
    assert "run_1" not in _render_text(app.finalized[0].render())


def test_chat_first_agic_step_has_exactly_one_gap_after_submission() -> None:
    app = FakeApp()
    app.live_blocks.append(blocks.RunControlBlock.create("hello"))

    events.handle_run_event(_run_begin(), app)
    events.handle_run_event(_model_step_begin(), app)

    transcript = "".join(
        _render_text(block.render())
        for block in (*app.finalized, *app.live_blocks)
        if not isinstance(block, blocks.RunSummaryBlock)
    )
    control_bottom = " " * 80
    assert f"{control_bottom}\n\n• Thinking..." in transcript
    assert f"{control_bottom}\n\n\n• Thinking..." not in transcript


def test_chat_uses_shared_progress_blocks_for_live_and_finalized_model_output() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(), app)
    events.handle_run_event(_model_step_begin(), app)
    assert [block.type for block in app.live_blocks] == [
        "ExecutionProgressBlock",
        "RunSummaryBlock",
    ]
    assert "• Thinking..." in _render_text(app.live_blocks[0].render())

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
    assert "Thinking..." not in streamed
    events.handle_run_event(
        PartEnd(
            step=StepPath.parse("run_1.1"),
            part=0,
            data=TextPart("drafting"),
        ),
        app,
    )
    events.handle_run_event(_model_step_end(output="drafting"), app)

    assert [block.type for block in app.live_blocks] == ["RunSummaryBlock"]
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
        "RunSummaryBlock",
    ]
    transcript = "".join(_render_text(block.render()) for block in app.finalized)
    assert "• drafting\n\n∎ run_1 succeeded" in transcript


def test_chat_tool_call_only_model_step_vacates_live_position_for_tool() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(), app)
    events.handle_run_event(_model_step_begin(), app)
    summary = app.live_blocks[-1]
    assert [block.type for block in app.live_blocks] == [
        "ExecutionProgressBlock",
        "RunSummaryBlock",
    ]

    events.handle_run_event(
        StepEnd(
            step=StepPath.parse("run_1.1"),
            kind="model",
            status="succeeded",
            output=_parts(
                ToolCallPart(
                    tool_call_id="call_1",
                    tool_name="shell__execute",
                    tool_family="shell",
                    input={"command": "echo ok"},
                )
            ),
            noted=ModelStepNoted(tokens=ModelTokenCount(input=12, output=3)),
        ),
        app,
    )

    assert app.finalized == []
    assert app.live_blocks == [summary]

    events.handle_run_event(_tool_step_begin(step_index=2), app)

    assert [block.type for block in app.live_blocks] == [
        "ExecutionProgressBlock",
        "RunSummaryBlock",
    ]
    assert app.live_blocks[-1] is summary
    rendered = _render_text(app.live_blocks[0].render())
    assert "executing shell__execute" in rendered
    assert "Thinking..." not in rendered
    assert "requested" not in rendered


def test_chat_flow_keeps_one_blank_row_at_each_finalized_boundary() -> None:
    app = FakeApp()
    app.live_blocks.append(blocks.RunControlBlock.create("map the items"))

    events.handle_run_event(_run_begin(runnable_kind="flow"), app)
    events.handle_run_event(_flow_step_begin(), app)
    events.handle_run_event(_flow_step_end(), app)
    events.handle_run_event(_run_end(status="succeeded", output_step_index=1), app)

    transcript = "".join(_render_text(block.render()) for block in app.finalized)
    control_bottom = " " * 80
    assert f"{control_bottom}\n\n[1] Run summarize" in transcript
    assert f"{control_bottom}\n\n\n[1] Run summarize" not in transcript
    assert "[1] Run summarize for each item, up to 2 at once\n\n• Mapped" in (
        transcript
    )
    assert "items in parallel\n\n∎ run_1 succeeded" in transcript
    assert "items in parallel\n\n\n∎ run_1 succeeded" not in transcript


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
    assert _render_text(app.live_blocks[0].render()).startswith("\n• Heading")

    events.handle_run_event(
        PartDelta(step=path, part=0, delta=TextDelta("Paragraph")),
        app,
    )
    assert len(app.finalized) == 1
    assert _render_text(app.finalized[0].render()).startswith("\n• Heading")
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
    assert [block.type for block in app.live_blocks] == ["RunSummaryBlock"]

    events.handle_run_event(
        _model_step_end(output="# Heading\n\nParagraph"),
        app,
    )
    assert len(app.finalized) == 2


def test_chat_parallel_terminal_update_replaces_every_lane_atomically() -> None:
    app = FakeApp()
    par_path = StepPath.parse("run_1.1")

    events.handle_run_event(_run_begin(runnable_kind="flow"), app)
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
    assert "0 | #0 | • Thinking..." in live
    assert "1 | #1 | • Thinking..." in live

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
            error=Pointer.step(StepPath.parse("run_child_0.0"), "error"),
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

    assert [block.type for block in app.live_blocks] == ["RunSummaryBlock"]
    finalized = _render_text(app.finalized[-1].render())
    assert (
        "• Parallel execution stopped: 0/2 succeeded, 1 failed, and 1 was canceled"
    ) in finalized
    assert "parallel step stopped because lane 0 (#0) failed" in finalized
    assert "0 | #0 | • failed model unavailable" in finalized


@pytest.mark.parametrize(
    "rows",
    [
        (ProgressRow("• executed web.search"), ProgressRow("  5 results")),
        (ProgressRow("[0] Run summarize"), ProgressRow("")),
        (
            ProgressRow(
                "  2.0s · 1 run",
                right_text="run_1.0",
            ),
        ),
    ],
)
def test_script_and_chat_sinks_preserve_the_same_semantic_rows(
    rows: tuple[ProgressRow, ...],
) -> None:
    progress = ProgressBlock("step:run_1.0", rows)
    stream = StringIO()
    ProgressConsole(stream, width=80).apply(ProgressUpdate(committed=(progress,)))
    chat = blocks.ExecutionProgressBlock(progress)

    assert stream.getvalue() == _render_text(chat.render()).removeprefix("\n")


def test_chat_submission_has_no_status_before_run_begin() -> None:
    block = blocks.RunControlBlock.create("hello")

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
    ]


def test_chat_preaccept_error_does_not_render_a_failed_run() -> None:
    app = FakeApp()
    app.live_blocks.append(blocks.RunControlBlock.create(":flow missing\n\nInput"))

    handled = events.handle_run_error(app, "Runnable not found: missing")

    assert handled is True
    assert app.live_blocks == []
    assert [block.type for block in app.finalized] == [
        "RunControlBlock",
        "SubmissionErrorBlock",
    ]
    rendered = "\n".join(_render_text(block.render()) for block in app.finalized)
    assert "• Runnable not found: missing" in rendered
    assert "starting" not in rendered
    assert "run failed" not in rendered
    assert app.finished
    transcript = "".join(_render_text(block.render()) for block in app.finalized)
    assert "\n\n• Runnable not found: missing" in transcript
    assert "\n\n\n• Runnable not found: missing" not in transcript


def test_chat_cancel_updates_existing_run_summary_block() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(), app)
    summary = cast(blocks.RunSummaryBlock, app.live_blocks[0])
    summary.mark_canceling()

    assert [block.type for block in app.live_blocks] == ["RunSummaryBlock"]
    assert "canceling" in _render_text(app.live_blocks[0].render())


def test_chat_run_summary_block_shows_canceling_then_canceled() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(), app)
    summary = cast(blocks.RunSummaryBlock, app.live_blocks[0])
    summary.mark_canceling()

    assert [block.type for block in app.live_blocks] == ["RunSummaryBlock"]
    assert "canceling" in _render_text(app.live_blocks[0].render())

    events.handle_run_event(_run_end(status="canceled"), app)

    assert app.live_blocks == []
    assert [block.type for block in app.finalized] == ["RunSummaryBlock"]
    rendered = _render_text(app.finalized[0].render())
    lines = rendered.splitlines()
    assert lines[0] == ""
    assert lines[1].startswith("∎ run_1 canceled  ")
    assert lines[1].endswith("3s")
    assert rendering.display_len(lines[1]) == 80
    assert lines[2] == ""


def test_chat_root_footer_counts_child_runs_for_any_runnable_kind() -> None:
    block = blocks.RunSummaryBlock.create(_run_begin(), max_width=72)
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
    assert "6 runs 8 models 2 tools" in rendered
    assert all(rendering.display_len(line) <= 72 for line in lines)
    assert all(not line.endswith("·") for line in lines)
    assert lines[0].startswith("∎ run_1")
    assert all(line.startswith("  ") for line in lines[1:])


def test_chat_root_footer_omits_zero_child_runs() -> None:
    block = blocks.RunSummaryBlock.create(_run_begin())
    block.update(_run_end(status="succeeded"))
    block.set_metrics(Metrics(runs=1))

    rendered = _render_text(block.render())

    assert "0 runs" not in rendered


def test_progress_marks_complete_zero_price_as_exact() -> None:
    metrics = Metrics()
    metrics.record_step(
        StepEnd(
            step=StepPath.parse("run_1.0"),
            kind="model",
            status="succeeded",
            noted=ModelStepNoted(
                accounting=ModelAccounting(
                    input_tokens=3800,
                    output_tokens=120,
                    estimate=ModelCost(
                        amount="0",
                        currency="USD",
                        complete=True,
                        lines=(
                            ModelCostLine(
                                meter="input",
                                quantity="3800",
                                unit="token",
                                rate="0",
                                per="1000000",
                                amount="0",
                            ),
                            ModelCostLine(
                                meter="output",
                                quantity="120",
                                unit="token",
                                rate="0",
                                per="1000000",
                                amount="0",
                            ),
                        ),
                    ),
                    meters=(
                        ModelUsageMeter(
                            name="output.reasoning",
                            quantity="0",
                            unit="token",
                        ),
                    ),
                    selected="estimated",
                )
            ),
        )
    )

    assert metrics.facts(include_runs=False) == [
        "1 model",
        "↑3.8k ↓120(0)",
    ]


@pytest.mark.parametrize(
    ("amount", "approximate", "expected"),
    [
        ("0", False, ""),
        ("0.030000", False, "$0.03"),
        ("0.042137", False, "$0.04"),
        ("1.200000", False, "$1.2"),
        ("0.001276", False, "$0.0013"),
        ("0.0000124", False, "<$0.0001"),
        ("0.010762", True, "≈$0.01"),
        ("0.0000124", True, "≲$0.0001"),
    ],
)
def test_progress_cost_uses_adaptive_precision(
    amount: str,
    approximate: bool,
    expected: str,
) -> None:
    metrics = Metrics(
        cost=Decimal(amount),
        cost_known=True,
        cost_approximate=approximate,
    )

    assert metrics.facts(include_runs=False) == ([expected] if expected else [])


def test_progress_marks_partial_reasoning_as_a_lower_bound() -> None:
    metrics = Metrics()
    for accounting in (
        ModelAccounting(
            input_tokens=1000,
            output_tokens=200,
            meters=(
                ModelUsageMeter(
                    name="output.reasoning",
                    quantity="150",
                    unit="token",
                ),
            ),
        ),
        ModelAccounting(input_tokens=100, output_tokens=20),
    ):
        metrics.record_step(
            StepEnd(
                step=StepPath.parse(f"run_1.{metrics.model_calls}"),
                kind="model",
                status="succeeded",
                noted=ModelStepNoted(accounting=accounting),
            )
        )

    assert metrics.facts(include_runs=False) == [
        "2 models",
        "↑1.1k ↓220(150+)",
    ]


def test_chat_root_footer_keeps_short_facts_inline() -> None:
    block = blocks.RunSummaryBlock.create(_run_begin(run_id="run_pmqv7gfc"))
    block.update(_run_end(run_id="run_pmqv7gfc", status="succeeded"))

    lines = [line for line in _render_text(block.render()).splitlines() if line]
    assert len(lines) == 1
    assert lines[0].startswith("∎ run_pmqv7gfc succeeded  ")
    assert lines[0].endswith("3s")
    assert "succeeded ·" not in lines[0]
    assert rendering.display_len(lines[0]) == 80


def test_chat_root_footer_wraps_every_facts_line_at_the_step_text_indent() -> None:
    block = blocks.RunSummaryBlock.create(_run_begin(), max_width=32)
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

    assert all(len(line) <= 32 for line in lines)
    assert lines[0] == "∎ run_1 failed"
    assert all(line.startswith("  ") for line in lines[1:])
    assert "3s" in lines[1]
    assert "6 runs" in "\n".join(lines)
    assert all("─" not in line for line in lines)


def test_chat_tool_step_dims_running_row_and_renders_terminal_surfaces() -> None:
    block = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_1.1",
            (
                ProgressRow(
                    "• executing shell__execute",
                    "active",
                    surface="tool_summary",
                ),
            ),
            gap_before=True,
        ),
        live=True,
        max_width=32,
    )

    running_segments = [
        segment
        for segment in rendering.render_segments(block.render(), width=80)
        if segment.text.strip()
    ]
    assert running_segments
    assert _render_text(block.render()).startswith("\n• executing shell__execute")
    assert all(
        segment.style is not None and segment.style.dim for segment in running_segments
    )
    assert all(
        segment.style is None or segment.style.bgcolor is None
        for segment in running_segments
    )

    block.live = False
    block.update(
        ProgressBlock(
            "step:run_1.1",
            (
                ProgressRow(
                    "• executed shell__execute",
                    surface="tool_summary",
                ),
                ProgressRow("  ok", surface="tool_detail"),
            ),
            gap_before=True,
        )
    )
    rendered = _render_text(block.render())
    lines = list(
        Segment.split_lines(rendering.render_segments(block.render(), width=80))
    )
    painted_lines = [
        [
            segment
            for segment in line
            if segment.style is not None and segment.style.bgcolor is not None
        ]
        for line in lines
    ]
    painted_lines = [line for line in painted_lines if line]

    assert rendered.startswith("\n• executed shell__execute\n\n")
    assert "ok" in rendered
    assert not any(character in rendered for character in "│└─┘▏▕▔")
    assert all(
        segment.style is None or segment.style.bgcolor is None for segment in lines[1]
    )
    assert all(not segment.text for segment in lines[0])
    assert all(not segment.text for segment in lines[2])
    assert len(painted_lines) == 3
    assert all(
        sum(len(segment.text) for segment in line) == 30 for line in painted_lines
    )
    background_numbers: list[set[int]] = []
    for line in painted_lines:
        numbers: set[int] = set()
        for segment in line:
            assert segment.style is not None
            assert segment.style.bgcolor is not None
            number = segment.style.bgcolor.number
            assert number is not None
            numbers.add(number)
        background_numbers.append(numbers)
    assert background_numbers == [{8}, {8}, {8}]

    painted_offsets: list[tuple[int, int]] = []
    for line in lines:
        offset = 0
        painted_start: int | None = None
        painted_end: int | None = None
        for segment in line:
            end = offset + len(segment.text)
            if segment.style is not None and segment.style.bgcolor is not None:
                painted_start = offset if painted_start is None else painted_start
                painted_end = end
            offset = end
        if painted_start is not None and painted_end is not None:
            painted_offsets.append((painted_start, painted_end))
    assert painted_offsets == [(2, 32), (2, 32), (2, 32)]

    rendered_lines = [
        "".join(segment.text for segment in line)
        for line in lines
        if any(segment.text for segment in line)
    ]
    padding = " " * 32
    assert rendered_lines[1] == padding
    assert rendered_lines[2].startswith("   ok")
    assert rendered_lines[2].endswith(" ")
    assert rendered_lines[3] == padding


def test_chat_tool_surfaces_wrap_within_the_configured_progress_width() -> None:
    block = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_1.1",
            (
                ProgressRow(
                    "• called read_text a-very-long-document-name.md",
                    surface="tool_summary",
                ),
                ProgressRow(
                    f"  {'x' * 40}",
                    surface="tool_detail",
                ),
            ),
        ),
        max_width=24,
    )

    lines = list(
        Segment.split_lines(rendering.render_segments(block.render(), width=80))
    )
    painted_lines = [
        line
        for line in lines
        if any(
            segment.style is not None and segment.style.bgcolor is not None
            for segment in line
        )
    ]

    assert len(painted_lines) > 2
    assert all(
        sum(len(segment.text) for segment in line) == 24 for line in painted_lines
    )
    background_widths: dict[int, set[int]] = {}
    for line in painted_lines:
        backgrounds = [
            segment
            for segment in line
            if segment.style is not None and segment.style.bgcolor is not None
        ]
        assert backgrounds
        widths: dict[int, int] = {}
        for segment in backgrounds:
            style = segment.style
            assert style is not None and style.bgcolor is not None
            number = style.bgcolor.number
            assert number is not None
            widths[number] = widths.get(number, 0) + len(segment.text)
        for number, width in widths.items():
            background_widths.setdefault(number, set()).add(width)
    assert background_widths == {8: {22}}

    rendered_lines = [
        "".join(segment.text for segment in line)
        for line in lines
        if any(segment.text for segment in line)
    ]
    assert all(len(line) <= 24 for line in rendered_lines)
    assert any(len(line) < 24 for line in rendered_lines)
    full_content = next(
        segment.text
        for line in lines
        for segment in line
        if segment.style is not None
        and segment.style.bgcolor is not None
        and segment.style.bgcolor.number == 8
        and "x" * 20 in segment.text
    )
    assert full_content == f" {'x' * 20} "
    assert rendered_lines.count(" " * 24) == 2
    assert not any(character in "".join(rendered_lines) for character in "│└─┘▏▕▔")


def test_chat_model_step_starts_after_a_blank_row() -> None:
    block = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_1.2",
            (
                ProgressRow(
                    "model answer",
                    format="markdown",
                    prefix="• ",
                ),
            ),
            gap_before=True,
        )
    )

    assert _render_text(block.render()).startswith("\n• model answer")


def test_chat_nested_headers_and_model_step_use_single_gaps() -> None:
    header = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_1.0.0",
            (
                ProgressRow("--- iteration 1 of 3 ---"),
                ProgressRow(""),
                ProgressRow("[0] Run review"),
                ProgressRow(""),
            ),
        )
    )
    model = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_review.0",
            (ProgressRow("• Thinking...", "active"),),
        ),
        live=True,
    )

    transcript = _render_text(header.render()) + _render_text(model.render())

    assert "--- iteration 1 of 3 ---\n\n[0] Run review" in transcript
    assert "[0] Run review\n\n• Thinking..." in transcript
    assert "[0] Run review\n\n\n• Thinking..." not in transcript


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
            gap_before=True,
        ),
        live=True,
    )

    rendered = _render_text(block.render(), width=32)

    lines = rendered.splitlines()
    assert len(lines) > 1
    assert rendered.startswith("\n• first")
    assert "..." not in rendered
    assert (
        " ".join(line.strip().removeprefix("• ") for line in lines if line) == content
    )


def test_chat_progress_width_is_bounded_on_a_wide_terminal() -> None:
    content = " ".join(f"word{index}" for index in range(40))
    block = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_1.0",
            (ProgressRow(f"• {content}"),),
            gap_before=True,
        )
    )

    rendered = _render_text(block.render(), width=160)
    lines = rendered.splitlines()

    assert lines[0] == ""
    assert all(rendering.display_len(line) <= 120 for line in lines)
    assert (
        " ".join(line.strip().removeprefix("• ") for line in lines if line) == content
    )


def test_chat_progress_width_honors_configured_maximum() -> None:
    content = " ".join(f"word{index}" for index in range(20))
    block = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_1.0",
            (ProgressRow(f"• {content}"),),
            gap_before=True,
        ),
        max_width=48,
    )

    rendered = _render_text(block.render(), width=160)
    lines = rendered.splitlines()

    assert lines[0] == ""
    assert all(rendering.display_len(line) <= 48 for line in lines)
    assert (
        " ".join(line.strip().removeprefix("• ") for line in lines if line) == content
    )


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
                ProgressRow(
                    "  27.0s · 5 runs",
                    "progress",
                    right_text="run_1.2",
                ),
            ),
        )
    )

    rendered = _render_text(block.render())

    assert "• 5 succeeded · 1 canceled" in rendered
    assert "statement failed" not in rendered
    assert "  27.0s · 5 runs" in rendered
    assert "run_1.2" in rendered
    footer_segments = [
        segment
        for segment in rendering.render_segments(block.render())
        if "27.0s" in segment.text or "run_1.2" in segment.text
    ]
    assert footer_segments
    assert all(
        segment.style is not None and segment.style.dim for segment in footer_segments
    )


@pytest.mark.parametrize(
    ("status", "marker_color"),
    [
        ("succeeded", None),
        ("failed", "red"),
        ("canceled", "yellow"),
    ],
)
def test_chat_run_footer_colors_marker_and_dims_caption(
    status: Literal["succeeded", "failed", "canceled"],
    marker_color: str | None,
) -> None:
    root_summary = blocks.RunSummaryBlock.create(_run_begin())
    root_summary.update(_run_end(status=status))
    segments = [
        segment
        for segment in rendering.render_segments(root_summary.render(), width=80)
        if segment.text.strip()
    ]

    assert _render_text(root_summary.render()).strip().startswith("∎ ")
    marker = next(segment for segment in segments if "∎" in segment.text)
    caption = next(segment for segment in segments if f"run_1 {status}" in segment.text)
    facts = next(segment for segment in segments if "3s" in segment.text)
    assert marker.style is not None
    assert not marker.style.dim
    if marker_color is None:
        assert marker.style.color is None
    else:
        assert marker.style.color is not None
        assert marker.style.color.name == marker_color
    assert caption.style is not None
    assert caption.style.dim
    assert not caption.style.bold
    assert caption.style.color is None
    assert facts.style is not None
    assert facts.style.dim
    assert facts.style.color is None
    assert not facts.style.bold


def test_chat_command_blocks_render_run_and_steer_states() -> None:
    run_control = blocks.RunControlBlock.create("hello")
    run_control.update(_run_begin())
    run_text = _render_text(run_control.render())
    assert f"{rendering.ACCENT_CELL} hello" in run_text
    assert ">" not in run_text
    assert "run_1" not in run_text

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
    assert run_text.splitlines() == [
        " " * 80,
        f"{rendering.ACCENT_CELL} hello" + " " * 73,
        " " * 80,
    ]
    assert steer_text.splitlines() == [
        "",
        " " * 80,
        f"{rendering.ACCENT_CELL} adjust" + " " * 72,
        " " * 80,
    ]

    run_fragments = rendering.renderable_to_prompt_toolkit(run_control.render())
    steer_fragments = rendering.renderable_to_prompt_toolkit(steer.render())
    stable_run = rendering.renderables_output([run_control.render()])
    run_segments = rendering.render_segments(run_control.render())
    run_message_segment = next(
        segment for segment in run_segments if "hello" in segment.text
    )
    assert run_message_segment.style is not None
    assert run_message_segment.style.color is None
    assert run_message_segment.style.dim is False
    run_prompt_accent = rendering._prompt_toolkit_color(
        Color.parse(rendering.RUN_CONTROL_ACCENT)
    )
    run_accent = next(
        fragment[0]
        for fragment in run_fragments
        if fragment[1] == rendering.ACCENT_CELL
        and f"bg:{run_prompt_accent}" in fragment[0]
    )
    steer_accent = next(
        fragment[0]
        for fragment in steer_fragments
        if fragment[1] == rendering.ACCENT_CELL
        and f"bg:{rendering.STEER_CONTROL_ACCENT}" in fragment[0]
    )
    run_message = next(
        fragment[0] for fragment in run_fragments if "hello" in fragment[1]
    )
    steer_message = next(
        fragment[0] for fragment in steer_fragments if "adjust" in fragment[1]
    )

    assert rendering.RUN_CONTROL_ACCENT == "bright_cyan"
    assert run_accent == f"bg:{run_prompt_accent} nodim"
    assert steer_accent == f"bg:{rendering.STEER_CONTROL_ACCENT} nodim"
    assert f"bg:{rendering.INPUT_BACKGROUND}" in run_message
    assert f"bg:{rendering.INPUT_BACKGROUND}" in steer_message
    assert "nodim" in run_message.split()
    assert "nodim" in steer_message.split()
    assert "\x1b[22m" in stable_run

    steer.update(_model_step_begin(step_index=2))
    assert _render_text(steer.render()) == steer_text


@pytest.mark.parametrize(
    ("block", "accent"),
    [
        (
            blocks.RunControlBlock.create("first\nsecond"),
            rendering.RUN_CONTROL_ACCENT,
        ),
        (
            blocks.RunSteerBlock.create(
                message="first\nsecond",
                run_id="run_1",
            ),
            rendering.STEER_CONTROL_ACCENT,
        ),
        (
            blocks.SlashBlock("first\nsecond", ()),
            rendering.QUICK_COMMAND_CONTROL_ACCENT,
        ),
    ],
)
def test_chat_two_line_control_bars_add_only_top_padding(
    block: blocks.MutableBlock | blocks.SlashBlock,
    accent: str,
) -> None:
    segments = rendering.render_segments(block.render(), width=20)
    accent_cells = [
        segment
        for segment in segments
        if segment.text == rendering.ACCENT_CELL
        and segment.style is not None
        and segment.style.bgcolor is not None
        and segment.style.bgcolor.get_truecolor().hex
        == Color.parse(accent).get_truecolor().hex
    ]

    assert len(accent_cells) == 3
    assert [
        line.rstrip()
        for line in _render_text(block.render(), width=20).splitlines()
        if line.strip()
    ] == ["  first", "  second"]


def test_chat_control_bar_uses_three_row_minimum() -> None:
    two_lines = _render_text(
        blocks.RunControlBlock.create("first\nsecond").render(),
        width=20,
    ).splitlines()
    three_lines = _render_text(
        blocks.RunControlBlock.create("first\nsecond\nthird").render(),
        width=20,
    ).splitlines()

    assert two_lines == [
        " " * 20,
        "  first" + " " * 13,
        "  second" + " " * 12,
    ]
    assert three_lines == [
        "  first" + " " * 13,
        "  second" + " " * 12,
        "  third" + " " * 13,
    ]


@pytest.mark.parametrize(
    ("message", "expected_rows"),
    [("x" * 40, 3), ("中文" * 20, 5)],
)
def test_chat_control_bar_wraps_every_physical_row(
    message: str,
    expected_rows: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(blocks, "terminal_width", lambda: 20)
    monkeypatch.setattr(rendering, "terminal_width", lambda: 20)

    rendered_lines = [
        line
        for line in _render_text(
            blocks.RunControlBlock.create(message).render(),
            width=20,
        ).splitlines()
        if line.strip()
    ]

    assert len(rendered_lines) == expected_rows
    assert all(line.startswith("  ") for line in rendered_lines)
    assert all(rendering.display_len(line) == 20 for line in rendered_lines)
    assert "".join(line[2:].rstrip() for line in rendered_lines) == message


def test_chat_prompt_keeps_its_run_control_accent() -> None:
    prompt = widgets.PromptBox(lambda _event: None, lambda: None)

    container = prompt.container()

    assert isinstance(container, VSplit)
    accent, content = container.children
    assert isinstance(accent, Window)
    assert accent.width == 1
    assert accent.style == "class:control.run"
    assert accent.char == rendering.ACCENT_CELL
    assert widgets._chat_ui_palette()["control.run"] == "bg:ansibrightcyan"
    assert rendering.CONTROL_BAR_BACKGROUND == "bright_black"
    assert rendering.INPUT_BACKGROUND == "ansibrightblack"
    assert (
        rendering._prompt_toolkit_color(Color.parse(rendering.CONTROL_BAR_BACKGROUND))
        == rendering.INPUT_BACKGROUND
    )
    assert isinstance(content, HSplit)
    input_row = content.children[1]
    assert isinstance(input_row, VSplit)
    left_padding = input_row.children[0]
    assert isinstance(left_padding, Window)
    assert left_padding.width == 1
    input_window = input_row.children[1]
    assert isinstance(input_window, Window)
    assert isinstance(input_window.content, BufferControl)
    right_padding = input_row.children[2]
    assert isinstance(right_padding, Window)
    assert right_padding.width == 1
    assert right_padding.style == "class:input"
    assert right_padding.char == " "
    assert input_window.content.input_processors is not None
    placeholder = input_window.content.input_processors[0]
    assert isinstance(placeholder, ConditionalProcessor)
    assert isinstance(placeholder.processor, AfterInput)
    assert placeholder.processor.style == "class:input.placeholder"
    assert placeholder.processor.text == "Ask or describe a task"
    assert placeholder.filter()
    assert widgets._chat_ui_palette()["input.placeholder"] == (
        f"fg:#b8b8b8 bg:{rendering.INPUT_BACKGROUND}"
    )

    prompt.buffer.text = "hello"

    assert not placeholder.filter()


def test_chat_queue_panel_defaults_to_expanded_with_only_a_focus_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel = widgets.QueuePanel(lambda: ["first", "second"])
    monkeypatch.setattr(panel, "_terminal_width", lambda: 100)

    fragments = panel._render()
    lines = "".join(text for _style, text in fragments).splitlines()

    assert panel.rows() == 4
    assert panel.expanded
    assert panel.selected_index == 0
    assert lines[0].strip() == "2 items queued"
    assert lines[0].index("2 items queued") == 43
    assert lines[1].startswith("  [1] first")
    assert lines[1].index("[1]") == lines[2].index("[2]") == 2
    assert lines[2].strip() == "[2] second"
    assert lines[3].strip() == "tab focus"
    assert not any(
        action in "".join(lines) for action in ("collapse", "edit", "steer", "delete")
    )
    assert all(get_cwidth(line) == 100 for line in lines)
    assert not any("queue.selected" in style for style, _text in fragments)


@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize("terminal_width", [80, 82, 100, 101, 122, 160])
def test_chat_queue_panel_splits_selected_entry_actions_from_panel_hints(
    monkeypatch: pytest.MonkeyPatch,
    focused: bool,
    terminal_width: int,
) -> None:
    text = "任务 preview " * 20
    panel = widgets.QueuePanel(lambda: [text, text])
    monkeypatch.setattr(panel, "_terminal_width", lambda: terminal_width)
    monkeypatch.setattr(panel, "_has_focus", lambda: focused)

    fragments = panel._render()
    lines = "".join(text for _style, text in fragments).splitlines()
    summary, selected, unselected, footer = lines
    assert summary.strip() == "2 items queued"
    assert fragments[0][0] == ("class:queue" if focused else "class:queue.info")
    assert summary.index("2 items queued") == (terminal_width - 14) // 2
    assert footer.strip() == (
        "↑↓ select · sp collapse · tab input" if focused else "tab focus"
    )
    assert selected.startswith("  [1] 任务 preview")
    assert unselected.startswith("  [2] 任务 preview")
    if focused:
        hint = "meta+enter steer · e edit · d delete  "
        assert selected.endswith(hint)
        assert selected[: selected.index(hint)].endswith("  ")
        assert selected[: selected.index(hint)].rstrip().endswith("…")
    else:
        assert selected[6:] == unselected[6:]
        assert selected.rstrip().endswith("…")
    assert not any(action in unselected for action in ("edit", "steer", "delete"))
    assert any("queue.selected" in style for style, _text in fragments) is focused
    assert [
        (style, text) for style, text in fragments if style.endswith(".number")
    ] == [
        ("class:queue.selected.number" if focused else "class:queue.number", " [1]"),
        ("class:queue.number", " [2]"),
    ]
    assert "›" not in "".join(lines)
    assert footer == footer.rstrip()
    assert all(get_cwidth(line) == terminal_width for line in lines)
    accent = ("class:queue", rendering.ACCENT_CELL)
    row_starts = [
        fragment
        for index, fragment in enumerate(fragments)
        if index == 0 or fragments[index - 1][1] == "\n"
    ]
    assert row_starts[1:-1] == [accent] * 2
    row_ends = [
        fragment
        for index, fragment in enumerate(fragments)
        if index == len(fragments) - 1 or fragments[index + 1][1] == "\n"
    ]
    assert row_ends[1:-1] == [accent] * 2


@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize("terminal_width", [40, 100, 101, 160])
def test_chat_queue_panel_collapses_to_one_centered_row_with_contextual_hints(
    monkeypatch: pytest.MonkeyPatch,
    focused: bool,
    terminal_width: int,
) -> None:
    panel = widgets.QueuePanel(lambda: ["first", "second"])
    monkeypatch.setattr(panel, "_terminal_width", lambda: terminal_width)
    monkeypatch.setattr(panel, "_has_focus", lambda: focused)

    assert panel.toggle_expanded()
    fragments = panel._render()
    lines = "".join(text for _style, text in fragments).splitlines()

    assert panel.rows() == 1
    assert panel.container().filter()
    assert panel.view.is_focusable()
    assert len(lines) == 1
    assert fragments[0][0] == ("class:queue" if focused else "class:queue.info")
    assert lines[0].index("2 items queued") == (terminal_width - 14) // 2
    hint = "tab focus"
    if focused:
        hint = "sp expand"
        if terminal_width >= 80:
            hint += " · tab input"
    assert lines[0].endswith(hint)
    assert lines[0].split("2 items queued", 1)[1].strip() == hint
    assert all(get_cwidth(line) == terminal_width for line in lines)
    assert not any(
        item in lines[0]
        for item in ("first", "second", "select", "edit", "delete", "steer")
    )


@pytest.mark.parametrize("terminal_width", [40, 100, 101])
@pytest.mark.parametrize("expanded", [False, True])
@pytest.mark.parametrize("focused", [False, True])
def test_chat_queue_panel_hints_align_with_status_right_edge(
    monkeypatch: pytest.MonkeyPatch,
    terminal_width: int,
    expanded: bool,
    focused: bool,
) -> None:
    panel = widgets.QueuePanel(lambda: ["first"])
    panel.expanded = expanded
    status = widgets.StatusBar("agic:chat", "model")
    monkeypatch.setattr(panel, "_terminal_width", lambda: terminal_width)
    monkeypatch.setattr(panel, "_has_focus", lambda: focused)
    monkeypatch.setattr(status, "_terminal_width", lambda: terminal_width)

    queue_line = "".join(text for _style, text in panel._render()).splitlines()[-1]
    for running in (False, True):
        status.set_running(running)
        status_line = "".join(text for _style, text in status._render())
        assert get_cwidth(queue_line) == get_cwidth(status_line) == terminal_width
        assert get_cwidth(queue_line.rstrip()) == get_cwidth(status_line.rstrip())
        assert not queue_line[-1].isspace()


@pytest.mark.parametrize("terminal_width", [25, 40, 80, 100])
def test_chat_queue_panel_wraps_footer_hints_in_narrow_terminals(
    monkeypatch: pytest.MonkeyPatch,
    terminal_width: int,
) -> None:
    panel = widgets.QueuePanel(lambda: ["first"])
    monkeypatch.setattr(panel, "_terminal_width", lambda: terminal_width)
    monkeypatch.setattr(panel, "_has_focus", lambda: True)

    lines = "".join(text for _style, text in panel._render()).splitlines()

    assert len(lines) == panel.rows() == (4 if terminal_width == 25 else 3)
    assert " · ".join(line.strip() for line in lines[2:]) == (
        "↑↓ select · sp collapse · tab input"
    )
    assert all(get_cwidth(line) == terminal_width for line in lines)
    assert all(line == line.rstrip() for line in lines[2:])
    assert lines[1].startswith("  [1] ")
    assert lines[0].index("1 item queued") == (terminal_width - 13) // 2
    if terminal_width >= 40:
        assert "meta+enter steer" in lines[1]


@pytest.mark.parametrize("terminal_width", [20, 25, 40, 50, 80, 101])
def test_chat_queue_preserves_action_gap_and_padding_when_truncating(
    monkeypatch: pytest.MonkeyPatch,
    terminal_width: int,
) -> None:
    panel = widgets.QueuePanel(lambda: ["任务 preview " * 20])
    monkeypatch.setattr(panel, "_terminal_width", lambda: terminal_width)
    monkeypatch.setattr(panel, "_has_focus", lambda: True)

    fragments = panel._render()
    lines = "".join(text for _style, text in fragments).splitlines()
    hint = next(
        text for style, text in fragments if style == "class:queue.selected.hint"
    )
    assert hint.strip()
    assert hint.endswith(" ")
    assert lines[1].endswith(hint + " ")
    assert lines[1][: lines[1].index(hint)].endswith("  ")
    assert lines[1].startswith("  [1] ")
    assert all(get_cwidth(line) == terminal_width for line in lines)


@pytest.mark.parametrize("message", ["👩‍💻" * 10, "❤️" * 25, "e\u0301" * 50])
def test_chat_queue_uses_prompt_toolkit_cell_width_for_combining_text(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    panel = widgets.QueuePanel(lambda: [message, message])
    monkeypatch.setattr(panel, "_terminal_width", lambda: 82)

    lines = "".join(text for _style, text in panel._render()).splitlines()

    assert all(get_cwidth(line) == 82 for line in lines)
    assert lines[1].startswith("  [1] ")
    assert lines[0].strip() == "2 items queued"


@pytest.mark.parametrize("expanded", [False, True])
@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize("terminal_width", [0, 1, 2, 3, 4, 12, 25, 40])
def test_chat_queue_fits_narrow_terminals_without_wrapping(
    monkeypatch: pytest.MonkeyPatch,
    expanded: bool,
    focused: bool,
    terminal_width: int,
) -> None:
    panel = widgets.QueuePanel(lambda: ["first", "second"])
    panel.expanded = expanded
    monkeypatch.setattr(panel, "_terminal_width", lambda: terminal_width)
    monkeypatch.setattr(panel, "_has_focus", lambda: focused)

    lines = "".join(text for _style, text in panel._render()).splitlines()
    assert len(lines) == panel.rows()
    assert all(get_cwidth(line) == terminal_width for line in lines)
    if expanded and terminal_width >= 12:
        assert lines[1].startswith("  [1]")


def test_chat_queue_panel_preserves_collapsed_state_until_empty() -> None:
    items = ["first", "second"]
    panel = widgets.QueuePanel(lambda: items)
    panel.move_selection(1)
    panel.toggle_expanded()

    items.append("third")
    assert panel.reconcile()
    assert not panel.expanded
    assert not panel.move_selection(1)
    assert panel.selected_index == 1

    items.pop(0)
    assert panel.reconcile(removed_index=0)
    assert panel.selected_index == 0
    assert not panel.expanded

    items.clear()
    assert not panel.reconcile()
    assert panel.expanded
    assert panel.rows() == 0
    assert panel._render() == []
    assert not panel.toggle_expanded()

    items.append("next")
    assert panel.reconcile()
    assert panel.expanded
    assert panel.rows() == 3


def test_chat_queue_panel_uses_a_full_width_window_and_distinct_background() -> None:
    panel = widgets.QueuePanel(lambda: ["first"])

    container = panel.container()

    assert isinstance(container, ConditionalContainer)
    assert isinstance(container.content, Window)
    assert container.content.content is panel.view
    assert container.content.width == panel.width
    palette = widgets._chat_ui_palette()
    assert palette["queue"] == "fg:#f5f5f5 bg:#3a3a3a"
    assert palette["queue.info"] == "fg:#b8b8b8 bg:#3a3a3a dim"
    assert palette["queue.selected"] == f"bg:{rendering.INPUT_BACKGROUND}"
    assert palette["queue.number"] == palette["queue.selected.number"] == "dim"
    assert palette["queue.selected.hint"] == "fg:#d0d0d0 dim"
    assert palette["queue.hint"] == "fg:#b8b8b8 dim"
    assert not any(name.startswith("queue.accent") for name in palette)


def test_chat_queue_panel_focus_selects_and_windows_queued_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [f"queued input {index}" for index in range(1, 7)]
    panel = widgets.QueuePanel(lambda: items)
    monkeypatch.setattr(panel, "_terminal_width", lambda: 100)

    monkeypatch.setattr(panel, "_has_focus", lambda: True)
    for _ in range(5):
        assert panel.move_selection(1)

    fragments = panel._render()
    rendered = "".join(text for _style, text in fragments)
    lines = rendered.splitlines()

    assert panel.rows() == 6
    assert panel.selected_index == 5
    assert lines[0].strip() == "6 items queued"
    assert "[3] queued input 3" in lines[1]
    assert "[6] queued input 6" in lines[4]
    assert lines[4].endswith("meta+enter steer · e edit · d delete  ")
    assert lines[5].strip() == "↑↓ select · sp collapse · tab input"
    assert "not shown" not in rendered
    assert all(get_cwidth(line) == 100 for line in lines)
    assert any(style == "class:queue.selected" for style, _text in fragments)
    assert fragments[0][0] == "class:queue"


def test_chat_queue_panel_reconciles_selection_after_removal() -> None:
    items = ["first", "second", "third"]
    panel = widgets.QueuePanel(lambda: items)
    panel.move_selection(2)

    items.pop(0)
    assert panel.reconcile(removed_index=0)
    assert panel.selected_index == 1

    items.pop(1)
    assert panel.reconcile(removed_index=1)
    assert panel.selected_index == 0

    items.clear()
    assert not panel.reconcile(removed_index=0)
    assert panel.selected_index is None
    assert panel.rows() == 0


def test_chat_prompt_submission_preserves_first_nonblank_line_indentation() -> None:
    submitted: list[ChatUIEvent] = []
    prompt = widgets.PromptBox(submitted.append, lambda: None)
    keys = KeyBindings()
    prompt.bind(keys)
    prompt.buffer.text = "\n \t\n  $review\n\n"

    binding = next(item for item in keys.bindings if item.keys == (Keys.ControlM,))
    cast(Any, binding.handler)(None)

    assert submitted == [ChatUIEvent("submit", "  $review")]
    assert prompt.history.get_strings() == []
    assert prompt.buffer.text == "\n \t\n  $review\n\n"

    prompt.accept_submission("  $review")

    assert prompt.history.get_strings() == ["  $review"]
    assert prompt.buffer.text == ""


def test_chat_prompt_rejected_submission_preserves_input_cursor_and_history() -> None:
    submitted: list[ChatUIEvent] = []
    prompt = widgets.PromptBox(submitted.append, lambda: None)
    keys = KeyBindings()
    prompt.bind(keys)
    prompt.buffer.text = "/unknown value"
    prompt.buffer.cursor_position = 4

    binding = next(item for item in keys.bindings if item.keys == (Keys.ControlM,))
    cast(Any, binding.handler)(None)

    assert submitted == [ChatUIEvent("submit", "/unknown value")]
    assert prompt.buffer.text == "/unknown value"
    assert prompt.buffer.cursor_position == 4
    assert prompt.history.get_strings() == []


def test_chat_prompt_meta_enter_emits_steer_without_accepting_the_draft() -> None:
    submitted: list[ChatUIEvent] = []
    prompt = widgets.PromptBox(submitted.append, lambda: None)
    keys = KeyBindings()
    prompt.bind(keys)
    prompt.buffer.text = "\n  /help\n"

    binding = next(
        item for item in keys.bindings if item.keys == (Keys.Escape, Keys.ControlM)
    )
    cast(Any, binding.handler)(None)

    assert submitted == [ChatUIEvent("steer", "  /help")]
    assert prompt.history.get_strings() == []
    assert prompt.buffer.text == "\n  /help\n"


def test_chat_prompt_ctrl_j_inserts_a_newline() -> None:
    prompt = widgets.PromptBox(lambda _event: None, lambda: None)
    keys = KeyBindings()
    prompt.bind(keys)
    prompt.buffer.text = "firstsecond"
    prompt.buffer.cursor_position = 5

    binding = next(item for item in keys.bindings if item.keys == (Keys.ControlJ,))
    cast(Any, binding.handler)(None)

    assert prompt.buffer.text == "first\nsecond"


def test_chat_prompt_bindings_cover_documented_shortcut_metadata() -> None:
    prompt = widgets.PromptBox(lambda _event: None, lambda: None)
    keys = KeyBindings()
    prompt.bind(keys)
    actual = {binding.keys for binding in keys.bindings}

    prompt_shortcuts = (
        shortcut
        for shortcut in shortcuts.CHAT_SHORTCUTS
        if shortcut is not shortcuts.SWITCH_AREA
    )
    for shortcut in prompt_shortcuts:
        for raw_binding in (*shortcut.bindings, *shortcut.optional_bindings):
            expected = KeyBindings()
            try:
                expected.add(*raw_binding)(lambda _event: None)
            except ValueError:
                continue
            assert expected.bindings[0].keys in actual, shortcut.name


def test_chat_tui_bindings_cover_all_documented_shortcut_metadata() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    assert app.app.key_bindings is not None
    actual = {binding.keys for binding in app.app.key_bindings.bindings}

    for shortcut in (*shortcuts.CHAT_SHORTCUTS, *shortcuts.QUEUE_SHORTCUTS):
        for raw_binding in (*shortcut.bindings, *shortcut.optional_bindings):
            expected = KeyBindings()
            try:
                expected.add(*raw_binding)(lambda _event: None)
            except ValueError:
                continue
            assert expected.bindings[0].keys in actual, shortcut.name


def test_chat_input_completion_keeps_namespaces_separate(tmp_path) -> None:
    (tmp_path / "README.md").write_text("read me", encoding="utf-8")
    completer = completion.ChatInputCompleter(resource_paths=lambda: [str(tmp_path)])
    completer.set_prompts(
        {
            "items": [
                {
                    "name": "review",
                    "params": [
                        {"name": "focus", "optional": False},
                        {"name": "tone", "optional": True},
                    ],
                }
            ]
        }
    )
    event = CompleteEvent(completion_requested=True)

    def values(source: str) -> list[str]:
        return [
            item.text for item in completer.get_completions(Document(source), event)
        ]

    assert values("/mo") == []
    assert values("$rev") == ["$review focus= tone="]
    assert values(":limit ti") == [":limit time="]
    assert values("  $rev") == []
    assert values("ordinary /mo") == []
    assert values("first\n/mo") == []
    assert values("@READ") == ["ME.md"]


def test_chat_prompt_box_enables_completion_for_authored_source() -> None:
    completer = completion.ChatInputCompleter()
    prompt = widgets.PromptBox(
        lambda _event: None,
        lambda: None,
        completer=completer,
    )

    assert prompt.buffer.completer is completer
    assert prompt.buffer.complete_while_typing()


def test_chat_prompt_grows_for_wrapped_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = widgets.PromptBox(lambda _event: None, lambda: None)
    monkeypatch.setattr(
        widgets.shutil,
        "get_terminal_size",
        lambda _fallback: SimpleNamespace(columns=10),
    )

    prompt.buffer.text = "123456"
    assert prompt.rows() == 3

    prompt.buffer.text = "1234567"
    assert prompt.rows() == 4

    prompt.buffer.text = "中文中文"
    assert prompt.rows() == 4

    prompt.buffer.text = "x" * 80
    assert prompt.rows() == widgets.MAX_INPUT_ROWS + 2


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
    assert durable_text == progress_text.removeprefix("\n")


def test_chat_fenced_code_preserves_one_rectangular_background() -> None:
    block = blocks.AssistantResponseBlock.from_parts(
        (TextPart("```python\nx = 1\n\ny = x + 1\n```"),),
        max_width=42,
    )

    lines = list(
        Segment.split_lines(rendering.render_segments(block.render(), width=42))
    )
    background_widths = [
        sum(
            len(segment.text)
            for segment in line
            if segment.style is not None and segment.style.bgcolor is not None
        )
        for line in lines
    ]

    assert background_widths == [40, 40, 40, 40, 40]
    assert {
        segment.style.bgcolor.number
        for line in lines
        for segment in line
        if segment.style is not None and segment.style.bgcolor is not None
    } == {8}
    assert all(
        segment.style is None
        or segment.style.color is None
        or segment.style.color.type == ColorType.STANDARD
        for line in lines
        for segment in line
    )
    base_text = next(
        segment for line in lines for segment in line if "x" in segment.text
    )
    number = next(segment for line in lines for segment in line if "1" in segment.text)
    assert base_text.style is not None
    assert base_text.style.color is not None
    assert base_text.style.color.number == 15
    assert number.style is not None
    assert number.style.color is not None
    assert number.style.color.number == 12


def test_chat_inline_code_uses_cyan_on_the_terminal_default_background() -> None:
    block = blocks.AssistantResponseBlock.from_parts(
        (TextPart("before `value` after"),),
    )

    code = next(
        segment
        for segment in rendering.render_segments(block.render())
        if segment.text == "value"
    )

    assert code.style is not None
    assert code.style.bold
    assert code.style.color is not None
    assert code.style.color.number == 6
    assert code.style.bgcolor is None


@pytest.mark.parametrize(
    ("rich_color", "prompt_color"),
    [
        ("default", "ansidefault"),
        ("black", "ansiblack"),
        ("red", "ansired"),
        ("green", "ansigreen"),
        ("yellow", "ansiyellow"),
        ("blue", "ansiblue"),
        ("magenta", "ansimagenta"),
        ("cyan", "ansicyan"),
        ("white", "ansigray"),
        ("bright_black", "ansibrightblack"),
        ("bright_red", "ansibrightred"),
        ("bright_green", "ansibrightgreen"),
        ("bright_yellow", "ansibrightyellow"),
        ("bright_blue", "ansibrightblue"),
        ("bright_magenta", "ansibrightmagenta"),
        ("bright_cyan", "ansibrightcyan"),
        ("bright_white", "ansiwhite"),
    ],
)
def test_chat_preserves_rich_ansi_color_identity(
    rich_color: str,
    prompt_color: str,
) -> None:
    assert rendering._prompt_toolkit_color(Color.parse(rich_color)) == prompt_color


def test_chat_markdown_outputs_only_terminal_palette_colors() -> None:
    block = blocks.AssistantResponseBlock.from_parts(
        (
            TextPart(
                "# Heading\n\n*emphasis*\n\n- item\n\n> quote\n\n"
                "[link](https://example.com) and `value`\n\n"
                "```python\nx = 1\n```"
            ),
        ),
    )

    fragments = rendering.renderable_to_prompt_toolkit(block.render())
    styles = [fragment[0] for fragment in fragments if fragment[1].strip()]
    stable = rendering.renderables_output([block.render()])

    assert all("#" not in style for style in styles)
    assert any(f"bg:{rendering.INPUT_BACKGROUND}" in style for style in styles)
    assert "\x1b[38;2" not in stable
    assert "\x1b[48;2" not in stable


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


def test_chat_input_area_absorbs_live_progress_contraction() -> None:
    async def exercise() -> None:
        with create_app_session(input=DummyInput(), output=_TerminalOutput()):
            app = tui.ChatTuiApp(
                thread_id=None,
                setting=FakeClient().initial_setting(),
                home="/tmp/agent",
                input_history=None,
                client=FakeClient(),
            )
            app.unfinalized_blocks.append(
                blocks.ExecutionProgressBlock(
                    ProgressBlock(
                        "step:run_1.0",
                        tuple(ProgressRow(f"line {index}") for index in range(8)),
                    )
                )
            )

            with set_app(app.app):
                expanded = _render_chat_layout(app)
                expanded_row = expanded.get_cursor_position(
                    app.app.layout.current_window
                ).y

                app.unfinalized_blocks[:] = [
                    blocks.ExecutionProgressBlock(
                        ProgressBlock(
                            "step:run_1.0",
                            (ProgressRow("remaining live row"),),
                        )
                    )
                ]
                contracted = _render_chat_layout(app)
                contracted_row = contracted.get_cursor_position(
                    app.app.layout.current_window
                ).y
                assert contracted_row == expanded_row
                contracted_spacer_rows = app._input_spacer_rows()

                app.unfinalized_blocks.clear()
                empty = _render_chat_layout(app)
                empty_row = empty.get_cursor_position(app.app.layout.current_window).y
                assert empty_row == expanded_row
                empty_spacer_rows = app._input_spacer_rows()

            await app.app.cancel_and_wait_for_background_tasks()
            assert contracted_spacer_rows == 7
            assert empty_spacer_rows == 8

    asyncio.run(exercise())


@pytest.mark.parametrize("selected_index", [0, 1, 2])
def test_chat_queue_joins_input_across_focus_expansion_and_terminal_resize(
    monkeypatch: pytest.MonkeyPatch,
    selected_index: int,
) -> None:
    async def exercise() -> None:
        output = _TerminalOutput()
        with create_app_session(input=DummyInput(), output=output):
            app = tui.ChatTuiApp(
                thread_id="term_busy",
                setting=FakeClient().initial_setting(),
                home="/tmp/agent",
                input_history=None,
                client=FakeClient(),
            )
            app.active_run_id = "run_busy"
            app.handle_submit("first")
            app.handle_submit("second")
            app.handle_submit("third")
            app.queue_panel.move_selection(selected_index)
            monkeypatch.setattr(
                app.status_bar, "_terminal_width", lambda: output.columns
            )
            app.prompt.buffer.text = "Keep typing"
            app.prompt.buffer.cursor_position = 4

            with set_app(app.app):
                app_style = app.app.style
                assert app_style is not None
                # Leave stable-footer space above Queue, never between it and Input.
                app._footer_row_floor = 20
                for columns in (160, 100, 82, 40, 101):
                    output.columns = columns
                    for expanded in (True, False, True):
                        app.queue_panel.expanded = expanded
                        for focused in (False, True, False):
                            app.app.layout.focus(
                                app.queue_panel.view if focused else app.prompt.buffer
                            )
                            screen = _render_chat_layout(app)
                            lines = {
                                row: "".join(
                                    screen.data_buffer[row][col].char
                                    for col in range(columns)
                                )
                                for row in range(screen.height)
                            }
                            summary_row = next(
                                row
                                for row, line in lines.items()
                                if "3 items queued" in line
                            )
                            input_row = next(
                                row
                                for row, line in lines.items()
                                if "Keep typing" in line
                            )
                            assert app.queue_panel.width() == columns
                            summary_col = (columns - 14) // 2
                            assert (
                                lines[summary_row].index("3 items queued")
                                == summary_col
                            )
                            panel_rows = 5 if expanded else 1
                            panel_top = summary_row
                            panel_bottom = panel_top + panel_rows - 1
                            assert app.queue_panel.rows() == panel_rows
                            assert input_row == panel_bottom + 2
                            assert app.queue_panel.selected_index == selected_index
                            if expanded:
                                assert lines[summary_row].strip() == "3 items queued"
                                hints = lines[panel_bottom].strip()
                                assert hints == (
                                    "↑↓ select · sp collapse · tab input"
                                    if focused
                                    else "tab focus"
                                )
                            else:
                                hint = "tab focus"
                                if focused:
                                    hint = "sp expand"
                                    if columns >= 80:
                                        hint += " · tab input"
                                assert lines[summary_row].endswith(hint)
                            status_row = input_row + 2
                            assert (
                                get_cwidth(lines[panel_bottom].rstrip())
                                == get_cwidth(lines[status_row].rstrip())
                                == columns
                            )
                            assert not lines[input_row - 1].strip()  # Input padding.
                            assert app._input_spacer_rows() > 0
                            assert app._available_live_rows() == (
                                30 - panel_rows - app.prompt.rows() - 1
                            )

                            def background(row: int, col: int) -> str | None:
                                return app_style.get_attrs_for_style_str(
                                    screen.data_buffer[row][col].style
                                ).bgcolor

                            input_background = background(input_row - 1, 1)
                            assert input_background == rendering.INPUT_BACKGROUND
                            queue_background = background(summary_row, 1)
                            assert queue_background == "3a3a3a"
                            assert queue_background != input_background
                            for row in range(panel_top, panel_top + panel_rows):
                                assert background(row, 0) == queue_background
                                assert background(row, columns - 1) == queue_background
                                selected = (
                                    expanded
                                    and focused
                                    and row == panel_top + 1 + selected_index
                                )
                                row_background = (
                                    input_background if selected else queue_background
                                )
                                assert all(
                                    background(row, col) == row_background
                                    for col in range(1, columns - 1)
                                )
                                if expanded and panel_top < row < panel_bottom:
                                    number_attrs = app_style.get_attrs_for_style_str(
                                        screen.data_buffer[row][2].style
                                    )
                                    entry_attrs = app_style.get_attrs_for_style_str(
                                        screen.data_buffer[row][6].style
                                    )
                                    assert number_attrs.dim
                                    assert not number_attrs.bold
                                    assert not entry_attrs.dim
                                    assert not entry_attrs.bold
                                    assert (
                                        number_attrs.color
                                        == entry_attrs.color
                                        == "f5f5f5"
                                    )
                                if selected:
                                    assert lines[row].startswith(
                                        f"  [{selected_index + 1}] "
                                    )
                                    assert lines[row].index(
                                        f"[{selected_index + 1}]"
                                    ) == lines[input_row].index("Keep typing")
                                    entry_hint_attrs = (
                                        app_style.get_attrs_for_style_str(
                                            screen.data_buffer[row][columns - 2].style
                                        )
                                    )
                                    assert entry_hint_attrs.dim
                                    assert entry_hint_attrs.color == "d0d0d0"
                                    if columns >= 80:
                                        assert lines[row].endswith(
                                            "meta+enter steer · e edit · d delete  "
                                        )
                            if not focused:
                                assert not any(
                                    action in lines[row]
                                    for row in range(panel_top, panel_bottom + 1)
                                    for action in (
                                        "e edit",
                                        "d delete",
                                        "meta+enter steer",
                                    )
                                )
                            summary_attrs = app_style.get_attrs_for_style_str(
                                screen.data_buffer[summary_row][summary_col].style
                            )
                            hint_attrs = app_style.get_attrs_for_style_str(
                                screen.data_buffer[panel_bottom][columns - 2].style
                            )
                            assert summary_attrs.color == (
                                "f5f5f5" if focused else "b8b8b8"
                            )
                            assert summary_attrs.dim is not focused
                            assert not summary_attrs.bold
                            assert hint_attrs.color == "b8b8b8"
                            assert hint_attrs.dim
                            assert background(input_row - 2, 0) == queue_background
                            assert background(input_row - 1, 0) == "ansibrightcyan"
                            assert background(input_row, 0) == "ansibrightcyan"
                            assert screen.show_cursor is not focused
                            assert app.prompt.buffer.cursor_position == 4
                            if not focused:
                                cursor = screen.get_cursor_position(
                                    app.app.layout.current_window
                                )
                                assert (cursor.x, cursor.y) == (6, input_row)

            await app.app.cancel_and_wait_for_background_tasks()

    asyncio.run(exercise())


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
        "/?",
        [
            "Slash commands act immediately.",
            "",
            "/help, /?                         Show help.",
            "/model [MODEL]                    List or switch models.",
        ],
    )
    rendered = _render_text(block.render(), width=69)
    rendered_lines = rendered.splitlines()
    all_segments = rendering.render_segments(block.render(), width=80)
    segments = [segment for segment in all_segments if segment.text.strip()]

    assert "▌" not in rendered
    assert not rendered_lines[0].strip()
    assert rendered_lines[1].startswith(f"{rendering.ACCENT_CELL} /?")
    assert ">" not in rendered_lines[1]
    assert not rendered_lines[2].strip()
    assert "  Slash commands act immediately." in rendered
    assert "/model [MODEL]" in rendered
    assert "List or switch models." in rendered
    assert not rendered.endswith("\n")
    command = next(segment for segment in segments if segment.text == "/model")
    argument = next(segment for segment in segments if segment.text == "[MODEL]")
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
        rendering.RUN_CONTROL_ACCENT,
        rendering.STEER_CONTROL_ACCENT,
    }
    assert command.style is not None
    assert command.style.color is not None
    assert argument.style is not None
    assert argument.style.color is None
    assert argument.style.dim is True
    assert all(segment.style is None or not segment.style.bold for segment in segments)


def test_chat_slash_summary_uses_two_space_indent_without_marker() -> None:
    block = blocks.SlashBlock(
        "/model openai/gpt-5 effort=high",
        ("Model set to openai/gpt-5 · high",),
        "success",
    )

    lines = _render_text(block.render(), width=69).splitlines()
    summary = next(line for line in lines if "Model set to" in line)

    assert summary == "  Model set to openai/gpt-5 · high"


def test_chat_main_help_styles_structure_and_honors_maximum_width() -> None:
    outcome = slashes.handle(None, QuickCommand("help"))  # type: ignore[arg-type]
    assert outcome is not None
    assert isinstance(outcome.content, slashes.SlashHelp)
    block = blocks.SlashHelpBlock(
        "/help",
        outcome.content,
        max_width=60,
    )

    rendered = _render_text(block.render(), width=100)
    segments = rendering.render_segments(block.render(), width=100)
    heading = next(
        segment for segment in segments if segment.text == "Session commands:"
    )
    command = next(segment for segment in segments if segment.text == "/model")
    argument = next(
        segment for segment in segments if segment.text == "[MODEL] [effort=VALUE]"
    )
    alias = next(segment for segment in segments if "(alias: /show)" in segment.text)
    narrow = _render_text(
        blocks.SlashHelpBlock("/help", outcome.content, max_width=24).render(),
        width=100,
    )
    narrow_words = " ".join(narrow.split())

    assert all(get_cwidth(line) <= 60 for line in rendered.splitlines())
    assert all(get_cwidth(line) <= 24 for line in narrow.splitlines())
    assert "Set the session model or effort" in narrow_words
    assert "all available" in narrow_words
    assert "alias: /show" in narrow_words
    assert heading.style is not None and heading.style.bold
    assert command.style is not None and command.style.color is not None
    assert argument.style is not None and argument.style.dim
    assert alias.style is not None and alias.style.dim


def test_chat_slash_table_uses_neutral_headers_and_one_line_rows() -> None:
    block = blocks.SlashTableBlock(
        "/models",
        slashes.SlashTable(
            "Found 1 model",
            ("MODEL", "PRICE ($/1M)", "EFFORT"),
            (("openai/a-very-long-model *", "$ 1.25 / $10.00", "low, high"),),
            shrink_order=(2, 0, 1),
            protected_suffixes=(" *", None, None),
        ),
    )

    rendered = _render_text(block.render(), width=42)
    lines = rendered.splitlines()
    table_lines = [line for line in lines if line.startswith("  ")]
    segments = rendering.render_segments(block.render(), width=42)
    model_header = next(
        segment for segment in segments if segment.text.startswith("MODEL")
    )

    assert any("PRICE ($/1M)" in line and "EFFORT" in line for line in table_lines)
    assert any("─" in line for line in table_lines)
    assert any(line.rstrip().endswith(" *") or " *  " in line for line in table_lines)
    assert all(get_cwidth(line) <= 42 for line in table_lines)
    assert model_header.style is None or model_header.style.color is None


def test_chat_header_uses_wide_local_executor_layout() -> None:
    block = blocks.HeaderBlock(
        home="/tmp/toolang/agents/alice",
        executor_metadata=ChatExecutorMetadata(
            sandbox_selector="host",
            sandbox_detail=_HOST_DESCRIPTION,
        ),
        version_label="v0.1.0",
    )
    rendered = _render_text(block.render(), width=80)

    assert "████           ██" in rendered
    assert "⬤   ⬤" in rendered
    assert "Toolang" in rendered
    assert "0.1.0" in rendered
    assert "v0.1.0" in rendered
    assert "model" not in rendered
    assert "home" in rendered
    assert "/tmp/toolang/agents/alice" in rendered
    assert "executor" in rendered
    assert "embedded" in rendered
    lines = rendered.splitlines()
    assert lines[0] == ""
    assert lines[1].startswith("╭")
    home_line = next(line for line in lines if "/tmp/toolang/agents/alice" in line)
    executor_line = next(line for line in lines if "embedded" in line)
    sandbox_line = next(line for line in lines if _HOST_SANDBOX_VALUE in line)
    version_line = next(line for line in lines if "v0.1.0" in line)
    assert lines.index(version_line) < lines.index(executor_line)
    assert lines.index(executor_line) < lines.index(sandbox_line)
    assert lines.index(sandbox_line) < lines.index(home_line)
    assert next(index for index, line in enumerate(lines) if "████" in line) == next(
        index for index, line in enumerate(lines) if "embedded" in line
    )
    assert next(index for index, line in enumerate(lines) if "⬤" in line) == next(
        index for index, line in enumerate(lines) if _HOST_SANDBOX_VALUE in line
    )
    assert next(
        index for index, line in enumerate(lines) if "████" in line
    ) + 2 == next(
        index for index, line in enumerate(lines) if "/tmp/toolang/agents/alice" in line
    )
    assert home_line.index("home") == executor_line.index("executor")
    assert executor_line.index("executor") == sandbox_line.index("sandbox")
    value_column = home_line.index("/tmp/toolang/agents/alice")
    assert value_column == executor_line.index("embedded")
    assert value_column == sandbox_line.index(_HOST_SANDBOX_VALUE)
    bordered_lines = [line for line in lines if line]
    assert len({len(line) for line in bordered_lines}) == 1
    assert not bordered_lines[1].strip("│ ")
    assert "████" in bordered_lines[2]
    assert "executor" in bordered_lines[-5]
    assert "embedded" in bordered_lines[-5]
    assert "sandbox" in bordered_lines[-4]
    assert _HOST_SANDBOX_VALUE in bordered_lines[-4]
    assert "home" in bordered_lines[-3]
    assert not bordered_lines[-2].strip("│ ")
    assert lines[1].startswith("╭─ Toolang v0.1.0 ─")
    assert rendered.count("Toolang") == 1
    assert rendered.count("v0.1.0") == 1


def test_chat_header_stacks_without_clipping_in_a_narrow_terminal() -> None:
    rendered = _render_text(
        blocks.HeaderBlock(
            home="/tmp/toolang/agents/alice-with-a-long-home",
            executor_metadata=ChatExecutorMetadata(
                sandbox_selector="docker:python:3.13-slim",
                sandbox_detail="176191c1528b",
                endpoint="http://runtime.test:7001",
                version="v0.3.9",
            ),
            version_label="v0.1.0",
        ).render(),
        width=40,
    )

    lines = rendered.splitlines()
    logo_index = next(index for index, line in enumerate(lines) if "⬤" in line)
    toolang_index = next(index for index, line in enumerate(lines) if "Toolang" in line)
    executor_index = next(
        index for index, line in enumerate(lines) if "executor" in line
    )
    assert toolang_index < logo_index
    assert executor_index > logo_index + 1
    assert all(len(line) <= 40 for line in lines)
    bordered_lines = [line for line in lines if line]
    assert len({len(line) for line in bordered_lines}) == 1
    unwrapped = rendered.replace("\n", "").replace("│", "").replace(" ", "")
    assert "alice-with-a-long-home" in unwrapped
    assert "executorhttp://runtime.test:7001·v0.3.9" in unwrapped
    assert "sandboxdocker:python:3.13-slim·176191c1528b" in unwrapped
    assert rendered.count("·") == 2
    assert rendered.count("Toolang v0.1.0") == 1
    assert _CONTAINER_ID not in rendered


@pytest.mark.parametrize(
    "executor_metadata, version_label, expected_executor, expected_sandbox",
    (
        (
            ChatExecutorMetadata(
                sandbox_selector="host",
                sandbox_detail=_HOST_DESCRIPTION,
                endpoint="http://runtime.test:7001",
                version="v0.2.7-88-gc73484a9",
            ),
            "v0.2.7-87-g69439a4e",
            "http://runtime.test:7001 · v0.2.7-88-gc73484a9",
            _HOST_SANDBOX_VALUE,
        ),
        (
            ChatExecutorMetadata(
                sandbox_selector="docker:python:3.13-slim",
                sandbox_detail="176191c1528b",
                endpoint="http://runtime.test:7001",
                version="v0.3.9",
            ),
            "v0.3.8",
            "http://runtime.test:7001 · v0.3.9",
            "docker:python:3.13-slim · 176191c1528b",
        ),
        (
            ChatExecutorMetadata(
                sandbox_selector="host",
                sandbox_detail=_HOST_DESCRIPTION,
                endpoint="http://runtime.test:7001",
                version="v0.3.9",
            ),
            "v0.3.9",
            "http://runtime.test:7001",
            _HOST_SANDBOX_VALUE,
        ),
        (
            ChatExecutorMetadata(
                sandbox_selector="host",
                sandbox_detail=_HOST_DESCRIPTION,
                endpoint="http://runtime.test:7001",
                version="v0.3.9*",
            ),
            "v0.3.9*",
            "http://runtime.test:7001 · v0.3.9*",
            _HOST_SANDBOX_VALUE,
        ),
        (
            ChatExecutorMetadata(
                sandbox_selector="host",
                sandbox_detail=_HOST_DESCRIPTION,
                endpoint="http://runtime.test:7001",
                version="unknown",
            ),
            "unknown",
            "http://runtime.test:7001 · unknown",
            _HOST_SANDBOX_VALUE,
        ),
    ),
)
def test_chat_header_supports_remote_executor_identity(
    executor_metadata: ChatExecutorMetadata,
    version_label: str,
    expected_executor: str,
    expected_sandbox: str | None,
) -> None:
    rendered = _render_text(
        blocks.HeaderBlock(
            home="~/.toolang/agents/alice",
            executor_metadata=executor_metadata,
            version_label=version_label,
        ).render(),
        width=120,
    )

    executor_line = next(line for line in rendered.splitlines() if "executor" in line)
    executor_text = executor_line[executor_line.index("executor") :].rstrip("│ ")
    assert " ".join(executor_text.split()) == f"executor {expected_executor}"
    assert expected_sandbox is not None
    sandbox_line = next(line for line in rendered.splitlines() if "sandbox" in line)
    sandbox_text = sandbox_line[sandbox_line.index("sandbox") :].rstrip("│ ")
    assert " ".join(sandbox_text.split()) == f"sandbox {expected_sandbox}"


def test_chat_header_links_remote_endpoint_and_preserves_vertical_padding() -> None:
    local = blocks.HeaderBlock(
        home="~/.toolang/agents/eve",
        executor_metadata=ChatExecutorMetadata(
            sandbox_selector="host",
            sandbox_detail=_HOST_DESCRIPTION,
        ),
        version_label="v0.2.7-87-g69439a4e*",
    )
    remote = blocks.HeaderBlock(
        home="~/.toolang/agents/eve",
        executor_metadata=ChatExecutorMetadata(
            sandbox_selector="host",
            sandbox_detail=_HOST_DESCRIPTION,
            endpoint="http://localhost:7001",
            version="v0.3.0",
        ),
        version_label="v0.2.7-87-g69439a4e*",
    )
    sandboxed = blocks.HeaderBlock(
        home="~/.toolang/agents/eve",
        executor_metadata=ChatExecutorMetadata(
            sandbox_selector="docker:pyslim-3.11",
            sandbox_detail="2f0f8934abcd",
            endpoint="http://localhost:7001",
            version="v0.3.0",
        ),
        version_label="v0.2.7-87-g69439a4e*",
    )

    local_lines = _render_text(local.render(), width=120).splitlines()
    remote_lines = _render_text(remote.render(), width=120).splitlines()
    sandboxed_lines = _render_text(sandboxed.render(), width=120).splitlines()
    sandboxed_segments = rendering.render_segments(sandboxed.render(), width=120)

    assert "Toolang v0.2.7-87-g69439a4e*" in " ".join(" ".join(local_lines).split())
    assert len(remote_lines) == len(local_lines)
    assert len(sandboxed_lines) == len(remote_lines)
    for lines in (local_lines, remote_lines, sandboxed_lines):
        assert "Toolang v0.2.7-87-g69439a4e*" in lines[1]
        assert " ".join(lines).count("Toolang") == 1
        assert " ".join(lines).count("v0.2.7-87-g69439a4e*") == 1
        bordered = [line for line in lines if line]
        assert not bordered[1].strip("│ ")
        assert not bordered[-2].strip("│ ")
    ordered = [
        next(index for index, line in enumerate(sandboxed_lines) if value in line)
        for value in (
            "v0.2.7-87-g69439a4e*",
            "http://localhost:7001",
            "docker:pyslim-3.11",
            "~/.toolang/agents/eve",
        )
    ]
    assert ordered == sorted(ordered)
    endpoint = next(
        segment
        for segment in sandboxed_segments
        if segment.text == "http://localhost:7001"
    )
    separators = [segment for segment in sandboxed_segments if "·" in segment.text]
    assert endpoint.style is not None
    assert endpoint.style.link == "http://localhost:7001"
    assert len(separators) == 2
    assert all(
        segment.style is not None and segment.style.dim for segment in separators
    )


def test_chat_header_keeps_logo_cells_selectable_and_styles_metadata() -> None:
    segments = rendering.render_segments(
        blocks.HeaderBlock(
            home="/tmp/toolang/agents/alice",
            executor_metadata=ChatExecutorMetadata(
                sandbox_selector="host",
                sandbox_detail=_HOST_DESCRIPTION,
            ),
            version_label="v0.1.0",
        ).render(),
        width=80,
    )

    logo_blocks = [segment for segment in segments if "█" in segment.text]
    logo_dots = [segment for segment in segments if "⬤" in segment.text]
    caption = next(segment for segment in segments if "Toolang v0.1.0" in segment.text)
    keys = [
        next(segment for segment in segments if segment.text.strip() == key)
        for key in ("home", "executor", "sandbox")
    ]
    values = [
        next(segment for segment in segments if segment.text.strip() == value)
        for value in (
            "/tmp/toolang/agents/alice",
            "embedded",
            "host",
            _HOST_DESCRIPTION,
        )
    ]
    separators = [segment for segment in segments if "·" in segment.text]

    assert sum(segment.text.count("█") for segment in logo_blocks) == 16
    assert all(
        segment.style is not None
        and segment.style.color is not None
        and segment.style.color.name == "bright_cyan"
        and segment.style.bgcolor == segment.style.color
        and not segment.style.reverse
        for segment in logo_blocks
    )
    assert logo_dots
    assert all(
        segment.style is not None
        and segment.style.color is not None
        and segment.style.color.name == "bright_cyan"
        and segment.style.bgcolor is None
        and not segment.style.reverse
        for segment in logo_dots
    )
    assert caption.style is not None
    assert not caption.style.bold
    assert not caption.style.dim
    assert caption.style.color is None
    assert all(segment.style is not None and segment.style.dim for segment in keys)
    assert all(
        segment.style is None or (not segment.style.bold and not segment.style.dim)
        for segment in values
    )
    assert len(separators) == 1
    assert separators[0].style is not None and separators[0].style.dim


def test_chat_model_label_uses_canonical_ref_and_reasoning_status() -> None:
    payload = {
        "default": "openai/gpt-5",
        "items": [
            {
                "ref": "openai/gpt-5",
                "name": "GPT-5",
                "provider": "openai",
                "parameters": {
                    "reasoning": {
                        "effort": ["low", "high"],
                        "applicable": True,
                    }
                },
            },
            {
                "ref": "openai/o3",
                "name": "o3",
                "provider": "openai",
                "parameters": {"reasoning": {"effort": ["high"], "applicable": False}},
            },
        ],
    }

    assert (
        slashes.chat_model_label(
            payload,
            SessionSetting(model=ModelRequest("openai/gpt-5"), runnable="agic:chat"),
        )
        == "openai/gpt-5 · auto"
    )
    assert (
        slashes.chat_model_label(
            payload,
            SessionSetting(model=ModelRequest("openai/o3"), runnable="agic:chat"),
        )
        == "openai/o3"
    )
    assert (
        slashes.chat_model_label(
            payload,
            SessionSetting(
                model=ModelRequest(
                    "openai/gpt-5",
                    ModelParameters(ReasoningParameters(effort="high")),
                ),
                runnable="agic:chat",
            ),
        )
        == "openai/gpt-5 · high"
    )
    assert (
        slashes.chat_model_label(
            payload,
            SessionSetting(model=None, runnable="agic:chat"),
        )
        == "[no models available]"
    )


@pytest.mark.parametrize(
    ("reasoning", "expected"),
    [
        (ReasoningParameters(effort="none"), "openai/gpt-5 · none"),
        (ReasoningParameters(budget_tokens=4096), "openai/gpt-5 · 4096"),
    ],
)
def test_chat_model_label_preserves_explicit_reasoning_values(
    reasoning: ReasoningParameters,
    expected: str,
) -> None:
    setting = SessionSetting(
        model=ModelRequest("openai/gpt-5", ModelParameters(reasoning)),
        runnable="agic:chat",
    )

    assert slashes.chat_model_label({"items": []}, setting) == expected


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
    status.set_activity(2, 1)
    next_frame = status._render()

    assert idle_text.startswith("■ agic:chat")
    assert idle_text.endswith("runtime model")
    assert running_text.startswith("◧ agic:chat running")
    assert "0s" not in running_text
    assert running_text.endswith("runtime model")
    assert idle[:3] == [
        ("class:status.marker", "■"),
        ("class:status", " "),
        ("class:status", "agic:chat"),
    ]
    assert running[:4] == [
        ("class:status.spinner", "◧"),
        ("class:status", " "),
        ("class:status", "agic:chat"),
        ("class:status.elapsed", " running"),
    ]
    assert next_frame[:4] == [
        ("class:status.spinner", "◨"),
        ("class:status", " "),
        ("class:status", "agic:chat"),
        ("class:status.elapsed", " 1s"),
    ]
    assert next_frame[-1] == ("class:status", "runtime model")
    assert status.spinner_index == 2
    assert status.elapsed_seconds == 1

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
    assert running.startswith("◧ agic:chat 18s")
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


def test_chat_status_bar_truncates_model_before_effort_suffix(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(widgets.StatusBar, "_terminal_width", staticmethod(lambda: 28))
    status = widgets.StatusBar(
        "agic:chat",
        "openai/a-very-long-model · high",
    )

    text = "".join(fragment for _style, fragment in status._render())

    assert get_cwidth(text) == 28
    assert text.endswith("… · high")


def test_chat_status_palette_uses_state_colors_for_markers() -> None:
    palette = widgets._chat_ui_palette()

    assert palette["status"] == ""
    assert palette["status.marker"] == "dim"
    assert palette["status.spinner"] == (
        f"fg:{rendering.RUN_CONTROL_ACCENT_PROMPT_TOOLKIT}"
    )
    assert palette["status.elapsed"] == "dim"
    assert palette["status.error.marker"] == "fg:ansired"
    assert palette["status.error"] == "fg:ansired"
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
        "circles": ("■", ("◐", "◓", "◑", "◒")),
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
        setting=FakeClient().initial_setting(),
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
            setting=FakeClient().initial_setting(),
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
            setting=FakeClient().initial_setting(),
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
                "class:status.spinner",
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
            setting=FakeClient().initial_setting(),
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
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )

    app.submit_run(
        QueuedCall(
            "hello",
            RunRequest(
                thread_id="term_request",
                request_id="term_request",
                runnable=RunnableRequest(
                    "agic:chat",
                    RunnableInputRaw(_="hello"),
                ),
                model=ModelRequest("openai/gpt-5"),
                policy=RunPolicy(),
            ),
        )
    )

    assert app.run_in_flight.is_set()
    assert app.status_bar.running

    app._finish_active_run()

    assert not app.run_in_flight.is_set()
    assert not app.status_bar.running


def test_chat_status_bar_error_uses_red_foreground_without_a_background(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(widgets.StatusBar, "_terminal_width", staticmethod(lambda: 40))
    status = widgets.StatusBar("agic:chat", "runtime model")
    status.set_error("No active run to steer")

    rendered = status._render()
    text = "".join(fragment for _style, fragment in rendered)

    assert rendered == [
        ("class:status.error.marker", "!"),
        ("class:status.error", " No active run to steer"),
        ("class:status", " " * 16),
    ]
    assert text.startswith("! No active run to steer")
    assert len(text) == 40


def test_chat_status_bar_error_is_single_line_and_truncated_to_width(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(widgets.StatusBar, "_terminal_width", staticmethod(lambda: 18))
    status = widgets.StatusBar("agic:chat", "runtime model")
    status.set_error("First line\nsecond line that must not render")

    text = "".join(fragment for _style, fragment in status._render())

    assert text == "! First line seco…"
    assert "\n" not in text
    assert get_cwidth(text) == 18


def test_chat_status_bar_persistent_error_survives_transient_updates() -> None:
    status = widgets.StatusBar("agic:chat", "openai/gpt-5")
    status.set_error("Connection lost · Reconnecting…", persistent=True)

    status.set_error("Temporary input problem")
    status.clear_transient_error()
    status.set_status("flow:research", "openai/o3")

    assert status.error_message == "Connection lost · Reconnecting…"
    status.clear_persistent_error()
    assert status.error_message == ""
    text = "".join(fragment for _style, fragment in status._render())
    assert text.startswith("■ flow:research")
    assert text.endswith("openai/o3")


def test_chat_tui_uses_truecolor_for_live_block_rendering() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )

    assert app.app.color_depth == ColorDepth.DEPTH_24_BIT


def test_chat_tui_resolves_only_the_selected_model_for_status() -> None:
    class DefaultOnlyClient(FakeClient):
        model_queries: list[Sequence[str] | None] = []

        def list_models(
            self,
            queries: Sequence[str] | None = None,
        ) -> dict[str, object]:
            self.model_queries.append(queries)
            assert queries == ("openai/gpt-5",)
            return {
                "default": "openai/gpt-5",
                "items": [
                    {
                        "ref": "openai/gpt-5",
                        "parameters": {
                            "reasoning": {"effort": ["low"], "applicable": True}
                        },
                    }
                ],
            }

        def list_runnables(self, kind: str) -> dict[str, object]:
            del kind
            raise AssertionError("status must not enumerate runnables")

    client = DefaultOnlyClient()
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=client,
    )

    assert app.status_bar.model_label == "openai/gpt-5 · auto"
    assert app.status_bar.runnable_label == "agic:chat"
    assert client.model_queries == [("openai/gpt-5",)]
    app.handle_run_event(_model_step_begin(model="deepseek/deepseek-chat"))
    assert app.status_bar.model_label == "openai/gpt-5 · auto"

    app.status_bar.set_error("Model selector matched no models")
    assert app.status_bar.error_message

    app.prompt.buffer.text = "retry"

    assert app.status_bar.error_message == ""


def test_chat_tui_omits_effort_when_model_metadata_lookup_fails() -> None:
    class UnavailableCatalogClient(FakeClient):
        def list_models(
            self,
            queries: Sequence[str] | None = None,
        ) -> dict[str, object]:
            assert queries == ("openai/gpt-5",)
            raise ToolangError("catalog unavailable")

    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=UnavailableCatalogClient(),
    )

    assert app.status_bar.model_label == "openai/gpt-5"


def test_chat_tui_empty_enter_preserves_status_error() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.status_bar.set_error("Model selector matched no models")

    key_bindings = app.app.key_bindings
    assert key_bindings is not None
    bindings = key_bindings.get_bindings_for_keys((Keys.Enter,))

    assert bindings
    with set_app(app.app):
        active = [binding for binding in bindings if binding.filter()]
        assert active
        active[-1].handler(cast(Any, None))
    assert app.status_bar.error_message == "Model selector matched no models"


def test_chat_tui_navigation_clears_transient_status_error() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.status_bar.set_error("Model selector matched no models")

    key_bindings = app.app.key_bindings
    assert key_bindings is not None
    bindings = key_bindings.get_bindings_for_keys((Keys.Up,))

    assert bindings
    with set_app(app.app):
        active = [binding for binding in bindings if binding.filter()]
        assert active
        active[-1].handler(cast(Any, None))
    assert app.status_bar.error_message == ""


def test_chat_tui_first_escape_immediately_clears_status_error() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.status_bar.set_error("Model selector matched no models")
    app.app.timeoutlen = None

    with set_app(app.app):
        app.app.key_processor.feed(KeyPress(Keys.Escape))
        app.app.key_processor.process_keys()

    assert app.status_bar.error_message == ""


def test_chat_tui_treats_kind_specific_default_as_a_runnable_name() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=SessionSetting(
            model=ModelRequest("openai/gpt-5"), runnable="agic:default"
        ),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )

    assert app.status_bar.runnable_label == "agic:default"

    app.setting = SessionSetting(
        model=app.setting.model,
        runnable="agic:review",
    )
    assert app._runnable_label() == "agic:review"

    app.setting = SessionSetting(
        model=app.setting.model,
        runnable="flow:research",
    )
    assert app._runnable_label() == "flow:research"


def test_chat_tui_uses_the_root_run_runnable_as_active_status(
    monkeypatch: Any,
) -> None:
    app = tui.ChatTuiApp(
        thread_id="term_status",
        setting=SessionSetting(
            model=ModelRequest("openai/gpt-5"), runnable="flow:research"
        ),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    monkeypatch.setattr(tui.events, "handle_run_event", lambda _event, _app: None)
    app.status_bar.set_active_runnable("flow:research")
    app.status_bar.set_running(True)

    app.handle_run_event(_run_begin(runnable_name="review"))
    app.handle_run_event(
        _run_begin(
            run_id="run_child",
            parent_run_id="run_1",
            runnable_kind="flow",
            runnable_name="child",
        )
    )

    assert app.status_bar.active_runnable_label == "agic:review"


def test_chat_tui_applies_default_settings_while_a_run_is_active() -> None:
    app = tui.ChatTuiApp(
        thread_id="term_status",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.active_run_id = "run_active"
    app.status_bar.set_active_runnable("agic:chat")
    app.status_bar.set_running(True)

    app.handle_submit("/flow research")

    runnable_changed = "".join(text for _style, text in app.status_bar._render())
    assert app.status_bar.active_runnable_label == "agic:chat"
    assert app.status_bar.runnable_label == "flow:research"
    assert runnable_changed.endswith("flow:research · openai/gpt-5")
    assert app.queue == []

    app.handle_submit("/model effort=high")

    model_changed = "".join(text for _style, text in app.status_bar._render())
    assert app.status_bar.active_runnable_label == "agic:chat"
    assert app.status_bar.model_label == "openai/gpt-5 · high"
    assert model_changed.endswith("flow:research · openai/gpt-5 · high")

    app.handle_submit("/agic chat")

    restored = "".join(text for _style, text in app.status_bar._render())
    assert restored.count("agic:chat") == 1
    assert restored.endswith("openai/gpt-5 · high")


def test_chat_default_settings_clear_explicit_model_and_runnable() -> None:
    surface = SessionSetting(
        model=ModelRequest("openai/gpt-5"),
        runnable="agic:chat",
    )
    updated = update_session_setting(
        surface=surface,
        current=SessionSetting(
            model=ModelRequest("openai/o3"),
            runnable="agic:review",
        ),
        update=RunOverride(
            model=ModelOverride(identity="default"),
            runnable="default",
        ),
    )

    assert updated == surface


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

        def run(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            started.set()

    client = LazyClient()
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=client,
    )
    monkeypatch.setattr(tui.rendering, "write_renderable", lambda *_args: None)

    app.handle_submit("/help")
    assert client.created == 0

    app.handle_submit("hello")
    assert started.wait(timeout=1)
    assert client.created == 1
    assert app.thread_id == "term_lazy"


def test_chat_thread_creation_error_is_a_submission_error() -> None:
    class FailingClient(FakeClient):
        def create_thread(self) -> str:
            raise ValueError("thread creation failed")

    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FailingClient(),
    )
    app.handle_submit("hello")

    assert app.status_bar.error_message == "thread creation failed"
    assert not app.status_bar.running
    assert not app.run_in_flight.is_set()


def test_chat_queue_captures_settings_at_submission_time() -> None:
    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.active_run_id = "run_busy"

    app.handle_submit("/model effort=low")
    app.handle_submit("first call")
    app.handle_submit("/model effort=high")
    app.handle_submit("second call")

    assert [item.source for item in app.queue] == ["first call", "second call"]
    assert [
        item.request.model.parameters.reasoning.effort
        for item in app.queue
        if item.request.model is not None
        and item.request.model.parameters.reasoning is not None
    ] == ["low", "high"]
    assert app.queue_panel.rows() == 4
    assert isinstance(app.app.layout.current_control, BufferControl)
    assert app.app.layout.current_control.buffer is app.prompt.buffer


def test_chat_tui_meta_enter_steers_literal_input_and_accepts_the_draft() -> None:
    steered = threading.Event()
    calls: list[tuple[str, str]] = []

    class RecordingClient(FakeClient):
        def steer(
            self,
            run_id: str,
            message: str,
            on_error: Callable[[str], None],
        ) -> None:
            del on_error
            calls.append((run_id, message))
            steered.set()

    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=RecordingClient(),
    )
    app.active_run_id = "run_busy"
    app.prompt.buffer.text = "/help"

    app.handle_ui_event(ChatUIEvent("steer", "/help"))

    assert steered.wait(timeout=1)
    assert calls == [("run_busy", "/help")]
    assert app.prompt.buffer.text == ""
    assert app.prompt.history.get_strings() == ["/help"]
    assert any(
        isinstance(block, blocks.RunSteerBlock) and block.message == "/help"
        for block in app.unfinalized_blocks
    )


def test_chat_tui_meta_enter_without_an_active_run_preserves_the_draft() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.prompt.buffer.text = "keep this draft"

    app.handle_ui_event(ChatUIEvent("steer", "keep this draft"))

    assert app.prompt.buffer.text == "keep this draft"
    assert app.prompt.history.get_strings() == []
    assert app.status_bar.error_message == "Start a run before steering"


def test_chat_tui_queue_panel_edit_refuses_to_overwrite_a_draft() -> None:
    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.active_run_id = "run_busy"
    app.handle_submit("queued input")
    app.prompt.buffer.text = "existing draft"

    app._edit_selected_queue_item()

    assert [item.source for item in app.queue] == ["queued input"]
    assert app.prompt.buffer.text == "existing draft"
    assert app.status_bar.error_message == (
        "Clear the input before editing a queued input"
    )

    app.prompt.buffer.text = ""
    app._edit_selected_queue_item()

    assert app.queue == []
    assert app.prompt.buffer.text == "queued input"
    assert isinstance(app.app.layout.current_control, BufferControl)
    assert app.app.layout.current_control.buffer is app.prompt.buffer


def test_chat_tui_queue_panel_steers_and_removes_only_after_local_acceptance() -> None:
    steered = threading.Event()

    class RecordingClient(FakeClient):
        def steer(
            self,
            run_id: str,
            message: str,
            on_error: Callable[[str], None],
        ) -> None:
            del run_id, message, on_error
            steered.set()

    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=RecordingClient(),
    )
    app.active_run_id = "run_busy"
    app.handle_submit("queued input")
    app.app.layout.focus(app.queue_panel.view)

    app._steer_selected_queue_item()

    assert steered.wait(timeout=1)
    assert app.queue == []
    assert isinstance(app.app.layout.current_control, BufferControl)
    assert app.app.layout.current_control.buffer is app.prompt.buffer

    rejected = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    rejected.queue.append(
        QueuedCall(
            "queued without run",
            FakeClient().build_request(
                "term_busy",
                RunOverride(),
                RunnableInputRaw(_="queued without run"),
                FakeClient().initial_setting(),
            ),
        )
    )
    rejected.queue_panel.reconcile()

    rejected._steer_selected_queue_item()

    assert [item.source for item in rejected.queue] == ["queued without run"]
    assert rejected.status_bar.error_message == "Start a run before steering"


def test_chat_tui_queue_panel_delete_clamps_selection() -> None:
    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.active_run_id = "run_busy"
    for source in ("first", "second", "third"):
        app.handle_submit(source)
    app.queue_panel.move_selection(2)

    app._delete_selected_queue_item()

    assert [item.source for item in app.queue] == ["first", "second"]
    assert app.queue_panel.selected_index == 1

    app._delete_selected_queue_item()
    app._delete_selected_queue_item()

    assert app.queue == []
    assert app.queue_panel.selected_index is None


def test_chat_tui_queue_shortcuts_switch_focus_and_move_selection() -> None:
    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.active_run_id = "run_busy"
    app.handle_submit("first")
    app.handle_submit("second")
    key_bindings = app.app.key_bindings
    assert key_bindings is not None

    def invoke(*binding_keys: Keys | str) -> None:
        bindings = key_bindings.get_bindings_for_keys(binding_keys)
        active = [binding for binding in bindings if binding.filter()]
        assert active
        active[-1].handler(cast(Any, None))

    with set_app(app.app):
        assert app.queue_panel._render()[0][0] == "class:queue.info"

        invoke(Keys.Tab)
        assert app.app.layout.current_control is app.queue_panel.view
        assert app.queue_panel._render()[0][0] == "class:queue"

        invoke(Keys.Down)
        assert app.queue_panel.selected_index == 1

        invoke(Keys.ControlP)
        assert app.queue_panel.selected_index == 0

        invoke(Keys.ControlN)
        assert app.queue_panel.selected_index == 1

        invoke(Keys.BackTab)
        assert isinstance(app.app.layout.current_control, BufferControl)
        assert app.app.layout.current_control.buffer is app.prompt.buffer
        assert app.queue_panel._render()[0][0] == "class:queue.info"

        invoke(Keys.BackTab)
        assert app.app.layout.current_control is app.queue_panel.view

        invoke(Keys.Tab)
        assert isinstance(app.app.layout.current_control, BufferControl)
        assert app.app.layout.current_control.buffer is app.prompt.buffer


def test_chat_tui_space_toggles_queue_and_tab_only_switches_focus() -> None:
    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.active_run_id = "run_busy"
    app.handle_submit("first")
    app.handle_submit("second")
    app.app.timeoutlen = None

    def press(key: Keys | str) -> None:
        app.app.key_processor.feed(KeyPress(key))
        app.app.key_processor.process_keys()

    async def exercise() -> None:
        with set_app(app.app):
            press(Keys.Tab)
            press(Keys.ControlN)
            assert app.queue_panel.selected_index == 1

            press(" ")
            assert not app.queue_panel.expanded
            assert app.queue_panel.rows() == 1
            assert app.app.layout.current_control is app.queue_panel.view

            for key in (
                Keys.Up,
                Keys.ControlP,
                Keys.Down,
                Keys.ControlN,
                "e",
                "d",
                Keys.Delete,
                Keys.Escape,
                Keys.Enter,
            ):
                press(key)
            app._edit_selected_queue_item()
            app._steer_selected_queue_item()
            app._delete_selected_queue_item()
            assert [item.source for item in app.queue] == ["first", "second"]
            assert app.queue_panel.selected_index == 1
            assert app.prompt.buffer.text == ""

            press(Keys.Tab)
            assert not app.queue_panel.expanded
            assert app.app.layout.current_control is not app.queue_panel.view
            press(" ")
            assert app.prompt.buffer.text == " "
            assert not app.queue_panel.expanded
            press("e")
            press("d")
            assert app.prompt.buffer.text == " ed"
            press(Keys.Escape)
            press(Keys.Enter)
            assert app.ui_events.get_nowait() == ChatUIEvent("steer", " ed")
            assert [item.source for item in app.queue] == ["first", "second"]
            app.prompt.buffer.reset()

            press(Keys.Tab)
            assert not app.queue_panel.expanded
            assert app.queue_panel.selected_index == 1
            assert app.app.layout.current_control is app.queue_panel.view

            press(" ")
            assert app.queue_panel.expanded
            assert app.queue_panel.selected_index == 1
            press(" ")
            app.handle_submit("third")
            assert not app.queue_panel.expanded
            assert app.app.layout.current_control is app.queue_panel.view
            app.queue.clear()
            app._reconcile_queue_panel()
            assert app.queue_panel.expanded
            assert app.app.layout.current_control is not app.queue_panel.view

        await app.app.cancel_and_wait_for_background_tasks()

    asyncio.run(exercise())


@pytest.mark.parametrize("expanded", [False, True])
def test_chat_tui_queue_focus_shortcuts_yield_to_input_completion(
    expanded: bool,
) -> None:
    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.active_run_id = "run_busy"
    app.handle_submit("queued input")
    app.queue_panel.expanded = expanded
    key_bindings = app.app.key_bindings
    assert key_bindings is not None
    app.prompt.buffer._set_completions([Completion("/help")])

    with set_app(app.app):
        tab_bindings = key_bindings.get_bindings_for_keys((Keys.Tab,))
        backtab_bindings = key_bindings.get_bindings_for_keys((Keys.BackTab,))

        assert not [binding for binding in tab_bindings if binding.filter()]
        assert not [binding for binding in backtab_bindings if binding.filter()]
        assert app.app.layout.current_control is not app.queue_panel.view
        assert app.queue_panel.expanded is expanded


def test_chat_tui_queue_steer_has_no_single_key_binding() -> None:
    assert shortcuts.QUEUE_STEER.bindings == shortcuts.STEER.bindings
    assert ("s",) not in shortcuts.QUEUE_STEER.bindings


def test_chat_queue_shortcut_help_includes_navigation_and_contextual_actions() -> None:
    lines = shortcuts.help_lines()
    queue_lines = lines[lines.index("Queue focused:") + 1 :]

    assert [line.split("  ", 1)[0] for line in queue_lines] == [
        "Space",
        "↑ (Ctrl+P)",
        "↓ (Ctrl+N)",
        "e",
        "Meta+Enter",
        "d (Del)",
    ]
    assert "Expand or collapse" in queue_lines[0]
    assert "Select previous input" in queue_lines[1]
    assert "Select next input" in queue_lines[2]


def test_chat_shortcuts_use_lowercase_inline_hints_without_changing_full_help() -> None:
    assert shortcuts.SWITCH_AREA.hint("Input") == "tab input"
    assert shortcuts.QUEUE_TOGGLE.hint("Expand") == "sp expand"
    assert shortcuts.QUEUE_EDIT.hint("Edit") == "e edit"
    assert shortcuts.QUEUE_STEER.hint("Steer") == "meta+enter steer"
    assert shortcuts.QUEUE_DELETE.hint("Delete") == "d delete"
    assert shortcuts.QUEUE_DELETE.help_label == "d (Del)"
    assert shortcuts.SWITCH_AREA.help_label == "Tab (Shift+Tab)"
    assert shortcuts.CANCEL_RUN.help_label == "Esc Esc"
    assert shortcuts.INSERT_NEWLINE.label == "Ctrl+J"
    assert "also Shift+Enter if supported" in shortcuts.INSERT_NEWLINE.summary
    assert shortcuts.QUEUE_EDIT.bindings == (("e",),)
    assert shortcuts.QUEUE_DELETE.bindings == (("d",), ("delete",))
    assert shortcuts.QUEUE_STEER.bindings == (("escape", "enter"),)

    lines = shortcuts.help_lines()
    summaries = [
        lines[index].index(shortcut.summary)
        for index, shortcut in enumerate(shortcuts.CHAT_SHORTCUTS)
    ]
    assert len(set(summaries)) == 1


def test_chat_tui_queue_blocks_mutations_after_ambiguous_acceptance() -> None:
    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.active_run_id = "run_busy"
    app.handle_submit("queued input")
    app.submission_blocked = "Restart Chat before submitting again."
    app.status_bar.set_error(app.submission_blocked, persistent=True)

    app._edit_selected_queue_item()
    app._steer_selected_queue_item()
    app._delete_selected_queue_item()

    assert [item.source for item in app.queue] == ["queued input"]
    assert app.prompt.buffer.text == ""
    assert app.status_bar.error_message == "Restart Chat before submitting again."


def test_chat_tui_keeps_the_queue_paused_while_remote_stream_is_disconnected() -> None:
    app = tui.ChatTuiApp(
        thread_id="term_remote",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.run_in_flight.set()

    app.handle_ui_event(ChatUIEvent("run_state", RunAccepted("run_remote")))
    app.handle_ui_event(
        ChatUIEvent(
            "run_state",
            RunDisconnected("run_remote", "waiting for durable state"),
        )
    )
    app.handle_submit("queued call")

    assert app.active_run_id == "run_remote"
    assert app.run_in_flight.is_set()
    assert [item.source for item in app.queue] == ["queued call"]
    assert app.status_bar.error_message == "Connection lost · Reconnecting…"


def test_chat_tui_recovers_from_durable_terminal_truth(
    monkeypatch: Any,
) -> None:
    rendered: list[str] = []
    monkeypatch.setattr(
        tui.rendering,
        "write_renderables",
        lambda values: rendered.extend(_render_text(value) for value in values),
    )
    app = tui.ChatTuiApp(
        thread_id="term_remote",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.run_in_flight.set()
    app.unfinalized_blocks.append(blocks.RunControlBlock.create("hello"))
    begin = _run_begin(run_id="run_remote")
    app.handle_ui_event(ChatUIEvent("run_state", RunAccepted("run_remote")))
    app.handle_run_event(begin)
    app.handle_ui_event(
        ChatUIEvent(
            "run_state",
            RunDisconnected("run_remote", "waiting for durable state"),
        )
    )
    app.prompt.buffer.text = "draft"
    assert app.status_bar.error_message == "Connection lost · Reconnecting…"
    app.handle_submit("/model effort=high")
    assert app.status_bar.error_message == "Connection lost · Reconnecting…"
    assert app.status_bar.model_label == "openai/gpt-5 · high"
    detail = RunDetail(
        id="run_remote",
        parent=None,
        thread_id="term_remote",
        root_run_id="run_remote",
        runnable_kind="agic",
        runnable_name="chat",
        call_kind="top",
        state=RunControlRefData(run="run_remote", index=0),
        occurrence=None,
        input_text="hello",
        summary="done",
        status="succeeded",
        error=None,
        ejected=None,
        created_at="2026-08-25T00:00:00Z",
        started_at="2026-08-25T00:00:00Z",
        finished_at="2026-08-25T00:00:01Z",
        updated_at="2026-08-25T00:00:01Z",
        control=RunControlRefData(run="run_remote", index=0),
        output=None,
        controls=[],
        steps=[],
    )

    app.handle_ui_event(ChatUIEvent("run_state", RunRecovered(detail)))

    output = "\n".join(rendered)
    assert "inspect the durable result" in output
    assert "with /output run_remote" in output
    assert "run_remote" in output
    assert not app.run_in_flight.is_set()
    assert app.active_run_id is None
    assert app.unfinalized_blocks == []
    assert app.status_bar.error_message == ""


def test_chat_tui_blocks_mutating_input_after_ambiguous_acceptance(
    monkeypatch: Any,
) -> None:
    rendered: list[str] = []
    monkeypatch.setattr(
        tui.rendering,
        "write_renderables",
        lambda values, **_kwargs: rendered.extend(
            _render_text(value) for value in values
        ),
    )
    app = tui.ChatTuiApp(
        thread_id="term_remote",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.run_in_flight.set()
    app.queue.append(
        QueuedCall(
            "already queued",
            FakeClient().build_request(
                "term_remote",
                RunOverride(),
                RunnableInputRaw(_="already queued"),
                FakeClient().initial_setting(),
            ),
        )
    )
    message = "Restart Chat before submitting again."

    app.handle_ui_event(ChatUIEvent("run_state", RunBlocked(None, message)))
    app.handle_submit("new call")
    app.handle_submit("/model test/model")
    app.handle_submit("/queue")
    app.handle_submit("/output run_remote")

    assert app.submission_blocked == message
    assert [item.source for item in app.queue] == ["already queued"]
    assert app.setting == FakeClient().initial_setting()
    assert app.run_in_flight.is_set()
    assert app.status_bar.error_message == f"Submissions paused: {message}"
    assert any("Queue commands" in value for value in rendered)
    assert any("durable result" in value for value in rendered)


def test_chat_tui_bare_model_command_does_not_open_or_load_a_picker(
    monkeypatch: Any,
) -> None:
    class CountingRemoteClient(FakeClient):
        model_reads = 0

        def list_models(
            self,
            queries: Sequence[str] | None = None,
        ) -> dict[str, object]:
            self.model_reads += 1
            return super().list_models(queries)

    client = CountingRemoteClient()
    app = tui.ChatTuiApp(
        thread_id="term_remote",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=client,
    )
    rendered: list[str] = []
    monkeypatch.setattr(
        tui.rendering,
        "write_renderables",
        lambda values, **_kwargs: rendered.extend(
            _render_text(value) for value in values
        ),
    )
    initial_reads = client.model_reads

    app.handle_submit("/model")

    assert app.status_bar.error_message == ""
    assert any("/model [MODEL] [effort=VALUE]" in value for value in rendered)
    assert any("Set the session model or effort" in value for value in rendered)
    assert client.model_reads == initial_reads


def test_chat_tui_routes_slash_shaped_parse_errors_to_scrollback(
    monkeypatch: Any,
) -> None:
    app = tui.ChatTuiApp(
        thread_id="term_remote",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    rendered: list[str] = []
    monkeypatch.setattr(
        tui.rendering,
        "write_renderables",
        lambda values, **_kwargs: rendered.extend(
            _render_text(value) for value in values
        ),
    )

    app.handle_submit("/help\nInput")

    assert app.status_bar.error_message == ""
    assert any(
        "Error: quick command cannot be combined with other input" in value
        for value in rendered
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("/", "Enter a command after / · See /? for help"),
        ("/missing value", "Unknown command /missing · See /? for help"),
        (":", "Enter a run override after : · See :? for help"),
        (":missing value", "Unknown run override :missing · See :? for help"),
        (
            ":model effort=high",
            "Add runnable input after the override · See :? for help",
        ),
    ],
)
def test_chat_tui_rejected_command_shaped_input_stays_editable_in_status(
    source: str,
    expected: str,
    monkeypatch: Any,
) -> None:
    rendered: list[str] = []
    monkeypatch.setattr(
        tui.rendering,
        "write_renderables",
        lambda values, **_kwargs: rendered.extend(
            _render_text(value) for value in values
        ),
    )
    app = tui.ChatTuiApp(
        thread_id="term_remote",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.prompt.buffer.text = source
    app.prompt.buffer.cursor_position = max(0, len(source) - 1)
    cursor = app.prompt.buffer.cursor_position

    app.handle_ui_event(ChatUIEvent("submit", source))

    assert app.prompt.buffer.text == source
    assert app.prompt.buffer.cursor_position == cursor
    assert app.prompt.history.get_strings() == []
    assert app.status_bar.error_message == expected
    assert rendered == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("/model", "/model [MODEL] [effort=VALUE]"),
        ("/help\nInput", "Error: quick command cannot be combined with other input"),
        ("/?", "Session commands:"),
        (":?", "Run overrides change settings for this run only."),
        ("/keys", "These shortcuts control interactive Chat."),
    ],
)
def test_chat_tui_recognized_help_usage_and_errors_enter_scrollback(
    source: str,
    expected: str,
    monkeypatch: Any,
) -> None:
    rendered: list[str] = []
    monkeypatch.setattr(
        tui.rendering,
        "write_renderables",
        lambda values, **_kwargs: rendered.extend(
            _render_text(value) for value in values
        ),
    )
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.prompt.buffer.text = source

    app.handle_ui_event(ChatUIEvent("submit", source))

    assert app.prompt.buffer.text == ""
    assert app.prompt.history.get_strings() == [source]
    assert app.status_bar.error_message == ""
    assert any(expected in value for value in rendered)
    assert app.thread_id is None


@pytest.mark.parametrize("source", (":model effort=high\nhello", "hello"))
def test_chat_tui_request_build_failure_retains_input_and_active_run(
    source: str,
) -> None:
    class FailingRequestClient(FakeClient):
        def build_request(
            self,
            thread_id: str,
            override: RunOverride,
            input: RunnableInputRaw,
            setting: SessionSetting,
        ) -> RunRequest:
            del thread_id, override, input, setting
            raise ValueError("selected model is unavailable")

    app = tui.ChatTuiApp(
        thread_id="term_remote",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FailingRequestClient(),
    )
    app.active_run_id = "run_active"
    app.run_in_flight.set()
    app.prompt.buffer.text = source
    app.prompt.buffer.cursor_position = 8
    cursor = app.prompt.buffer.cursor_position

    app.handle_ui_event(ChatUIEvent("submit", source))

    assert app.prompt.buffer.text == source
    assert app.prompt.buffer.cursor_position == cursor
    assert app.prompt.history.get_strings() == []
    assert app.status_bar.error_message == "selected model is unavailable"
    assert app.active_run_id == "run_active"
    assert app.run_in_flight.is_set()


def test_chat_tui_rejects_known_unsupported_colon_effort_in_status() -> None:
    class UnsupportedEffortClient(FakeClient):
        def build_request(
            self,
            thread_id: str,
            override: RunOverride,
            input: RunnableInputRaw,
            setting: SessionSetting,
        ) -> RunRequest:
            del override, setting
            return RunRequest(
                thread_id=thread_id,
                request_id="term_request",
                runnable=RunnableRequest("agic:chat", input),
                model=ModelRequest(
                    "openai/gpt-5",
                    ModelParameters(ReasoningParameters(effort="medium")),
                ),
                policy=RunPolicy(),
            )

    source = ":model effort=medium\nhello"
    app = tui.ChatTuiApp(
        thread_id="term_remote",
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=UnsupportedEffortClient(),
    )
    app.prompt.buffer.text = source
    app.prompt.buffer.cursor_position = 8

    consumed = app.handle_submit(source)

    assert consumed is False
    assert app.prompt.buffer.text == source
    assert app.prompt.buffer.cursor_position == 8
    assert app.status_bar.error_message == (
        "model openai/gpt-5 does not advertise reasoning effort "
        "'medium' (allowed: low, high)"
    )
    assert app.unfinalized_blocks == []


def test_chat_tui_uses_queued_runnable_snapshot_for_the_next_active_status() -> None:
    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=SessionSetting(
            model=ModelRequest("openai/gpt-5"), runnable="agic:chat"
        ),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    app.active_run_id = "run_busy"
    app.run_in_flight.set()
    app.status_bar.set_active_runnable("agic:chat")
    app.status_bar.set_running(True)
    app.queue.append(
        QueuedCall(
            "queued",
            RunRequest(
                thread_id="term_busy",
                request_id="term_queued",
                runnable=RunnableRequest(
                    "flow:research",
                    RunnableInputRaw(_="queued"),
                ),
                model=ModelRequest("openai/gpt-5"),
                policy=RunPolicy(),
            ),
        )
    )

    app._finish_active_run()

    assert app.status_bar.running
    assert app.status_bar.active_runnable_label == "flow:research"
    assert app.run_in_flight.is_set()


def test_chat_tui_empty_input_requires_two_interrupts_to_exit() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )

    assert app.handle_ui_event(ChatUIEvent("interrupt")) is False
    assert app.interrupt_exit_pending
    assert app.status_bar.error_message == "Press Ctrl+C again to exit"

    assert app.handle_ui_event(ChatUIEvent("interrupt")) is True


def test_chat_tui_typing_resets_pending_interrupt_exit() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )

    assert app.handle_ui_event(ChatUIEvent("interrupt")) is False
    app.prompt.buffer.text = "hello"

    assert not app.interrupt_exit_pending
    assert app.status_bar.error_message == ""


def test_chat_tui_clear_scrolls_one_separator_into_history_before_redrawing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    output = app.app.output
    actions: list[object] = []
    monkeypatch.setattr(app.app.renderer, "erase", lambda: actions.append("erase"))
    monkeypatch.setattr(
        type(output),
        "get_size",
        lambda _output: SimpleNamespace(rows=4, columns=80),
    )
    monkeypatch.setattr(
        type(output),
        "write_raw",
        lambda _output, value: actions.append(("write_raw", value)),
    )
    monkeypatch.setattr(
        type(output),
        "erase_screen",
        lambda _output: actions.append("erase_screen"),
    )
    monkeypatch.setattr(
        type(output),
        "cursor_goto",
        lambda _output, row, column: actions.append(("cursor_goto", row, column)),
    )
    monkeypatch.setattr(
        type(output),
        "flush",
        lambda _output: actions.append("flush"),
    )
    monkeypatch.setattr(
        app.app.renderer,
        "request_absolute_cursor_position",
        lambda: actions.append("request_cursor_position"),
    )
    app._footer_row_floor = 12

    app._handle_clear()

    assert app._footer_row_floor == 0
    assert actions == [
        "erase",
        ("write_raw", "\r\n" * 4),
        "erase_screen",
        ("cursor_goto", 0, 0),
        "flush",
        "request_cursor_position",
    ]


def test_chat_tui_removes_live_block_before_writing_scrollback(
    monkeypatch: Any,
) -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
    block = blocks.RunSummaryBlock.create(_run_begin())
    block.update(_run_end(status="canceled"))
    app.unfinalized_blocks.append(block)

    def write_renderables(renderables: Sequence[RenderableType | None]) -> None:
        del renderables
        assert block not in app.unfinalized_blocks

    monkeypatch.setattr(tui.rendering, "write_renderables", write_renderables)

    tui.ChatTuiAppContext(app).finalize_block(block)


def test_chat_tui_commits_live_finalization_in_one_terminal_transaction(
    monkeypatch: Any,
) -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
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
    monkeypatch.setattr(
        app.app.renderer,
        "request_absolute_cursor_position",
        lambda: order.append("request_cursor_position"),
    )
    app._footer_row_floor = 4

    def write_scrollback(renderables: list[RenderableType | None]) -> None:
        assert block not in app.unfinalized_blocks
        assert len(renderables) == 1
        order.append("write")

    monkeypatch.setattr(app, "_write_scrollback", write_scrollback)
    monkeypatch.setattr(app.app, "invalidate", lambda: order.append("invalidate"))

    tui.ChatTuiAppContext(app).finalize_block(block)
    assert order == []

    app._commit_ui_update()

    assert order == [
        "erase:False",
        "write",
        "request_cursor_position",
        "invalidate",
    ]
    assert app._footer_row_floor == 3
    assert app._pending_scrollback == []
    assert block not in app.unfinalized_blocks


def test_chat_tui_commits_slash_outcome_in_scrollback_transaction(
    monkeypatch: Any,
) -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )
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

    def write_scrollback(renderables: Sequence[RenderableType | None]) -> None:
        assert len(renderables) == 1
        assert "/model [MODEL] [effort=VALUE]" in _render_text(renderables[0])
        order.append("write")

    monkeypatch.setattr(app, "_write_scrollback", write_scrollback)
    monkeypatch.setattr(app.app, "invalidate", lambda: order.append("invalidate"))

    app.handle_submit("/model")

    assert order == []
    assert len(app._pending_scrollback) == 1

    app._commit_ui_update()

    assert order == ["erase:False", "write", "invalidate"]
    assert app._pending_scrollback == []


def test_chat_tui_adds_one_trailing_gap_to_summary_only_usage(
    monkeypatch: Any,
) -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
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

    app.handle_submit("/steer")

    assert len(written) == 1
    rendered = _render_text(written[0])
    assert rendered.endswith("\n\n")
    assert not rendered.endswith("\n\n\n")


def test_chat_tui_replaces_failed_model_live_state_in_scrollback_transaction(
    monkeypatch: Any,
) -> None:
    app = tui.ChatTuiApp(
        thread_id="thread_1",
        setting=FakeClient().initial_setting(),
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
    assert "Thinking..." in "".join(
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
    assert "Thinking..." not in writes[0]
    assert "You have no credits remaining." in writes[0]
    assert all(
        not isinstance(block, blocks.ExecutionProgressBlock)
        for block in app.unfinalized_blocks
    )


def test_chat_tui_output_command_renders_durable_markdown(
    monkeypatch: Any,
) -> None:
    app = tui.ChatTuiApp(
        thread_id="thread_1",
        setting=FakeClient().initial_setting(),
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

    app.handle_submit("/output run_saved")

    assert len(written) == 1
    rendered = _render_text(written[0])
    result_lines = [line.rstrip() for line in rendered.splitlines()]
    divider = "• run_saved output "
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
        segment for segment in segments if "run_saved output" in segment.text
    )
    rule = [segment for segment in segments if "─" in segment.text]
    response = next(segment for segment in segments if "durable result" in segment.text)
    assert marker.style is not None and marker.style.dim
    assert caption.style is not None and caption.style.dim
    assert rule and all(
        segment.style is not None and segment.style.dim for segment in rule
    )
    assert response.style is None or not response.style.dim


def test_chat_output_divider_truncates_without_wrapping(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(rendering, "terminal_width", lambda: 24)
    block = blocks.SlashResultBlock(
        message="/output run_saved_with_a_long_identifier",
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
    runnable_kind: str = "agic",
    runnable_name: str = "test",
) -> RunBegin:
    return RunBegin(
        run=run_id,
        control=ControlRef(run_id, 0),
        parent=(
            StepPath.parse(f"{parent_run_id}.2") if parent_run_id is not None else None
        ),
        started_at="2026-01-01T00:00:00Z",
        runnable=f"{runnable_kind}:{runnable_name}",
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
    setting: SessionSetting = field(
        default_factory=lambda: SessionSetting(
            model=ModelRequest("openai/gpt-5"),
            runnable="agic:chat",
        )
    )

    def get_setting(self) -> SessionSetting:
        return self.setting

    def set_setting(self, setting: SessionSetting) -> None:
        self.setting = setting

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

    def finalize_block(self, block: blocks.MutableBlock) -> None:
        if block in self.live_blocks:
            self.live_blocks.remove(block)
        self.finalized.append(block)

    def finish_run(self) -> None:
        self.active_run = None
        self.finished = True

    def refresh_status(self) -> None:
        pass

    def replace_input(self, text: str) -> None:
        del text

    def request_steer(self, message: str) -> None:
        del message

    def request_exit(self) -> None:
        pass


class FakeClient(ChatClient):
    executor_metadata = ChatExecutorMetadata(
        sandbox_selector="host",
        sandbox_detail=_HOST_DESCRIPTION,
    )

    def list_models(
        self,
        queries: Sequence[str] | None = None,
    ) -> dict[str, object]:
        del queries
        return {
            "default": "openai/gpt-5",
            "items": [
                {
                    "ref": "openai/gpt-5",
                    "name": "GPT-5",
                    "provider": "openai",
                    "parameters": {"reasoning": {"effort": ["low", "high"]}},
                }
            ],
        }

    def list_tools(
        self,
        queries: Sequence[str] | None = None,
    ) -> dict[str, object]:
        del queries
        return {"items": []}

    def list_caps(
        self,
        kind: str | None = None,
        queries: Sequence[str] | None = None,
    ) -> dict[str, object]:
        del kind, queries
        return {"items": []}

    def list_runnables(self, kind: str) -> dict[str, object]:
        if kind == "runnable":
            return {
                "default": "agic:chat",
                "items": [
                    {"kind": "agic", "name": "chat"},
                    {"kind": "flow", "name": "research"},
                ],
            }
        if kind == "agic":
            return {"items": [{"kind": "agic", "name": "chat"}]}
        if kind == "flow":
            return {"items": [{"kind": "flow", "name": "research"}]}
        return {"items": []}

    def create_thread(self) -> str:
        return "thread_1"

    def initial_setting(self) -> SessionSetting:
        return SessionSetting(
            model=ModelRequest("openai/gpt-5"),
            runnable="agic:chat",
        )

    def apply_setting(
        self,
        setting: SessionSetting,
        update: RunOverride,
        *,
        allowed_model_refs: Collection[str] | None = None,
        default_model_ref: str | None = None,
    ) -> SessionSetting:
        del allowed_model_refs, default_model_ref
        return update_session_setting(
            surface=self.initial_setting(),
            current=setting,
            update=update,
        )

    def build_request(
        self,
        thread_id: str,
        override: RunOverride,
        input: RunnableInputRaw,
        setting: SessionSetting,
    ) -> RunRequest:
        del override
        return RunRequest(
            thread_id=thread_id,
            request_id="term_request",
            runnable=RunnableRequest("agic:chat", input),
            model=setting.model,
            policy=RunPolicy(),
        )

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

    def run(
        self,
        request: RunRequest,
        on_event: Callable[[RunEvent], None],
        on_error: Callable[[str], None],
        on_state: Callable[[ChatRunState], None] | None = None,
    ) -> None:
        del request, on_event, on_error, on_state

    def cancel(
        self,
        run_id: str,
        on_error: Callable[[str], None],
    ) -> None:
        del run_id, on_error

    def steer(
        self,
        run_id: str,
        message: str,
        on_error: Callable[[str], None],
    ) -> None:
        del run_id, message, on_error
