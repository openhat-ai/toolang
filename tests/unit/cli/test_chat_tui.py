from __future__ import annotations
from toolang.lang.types import Array

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Collection, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from io import StringIO
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import create_app_session, set_app
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.input import DummyInput
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import ConditionalContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.processors import AfterInput, ConditionalProcessor
from prompt_toolkit.layout.screen import Screen
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.renderer import CPR_Support
from prompt_toolkit.styles import Attrs
from prompt_toolkit.utils import get_cwidth
from rich.cells import chop_cells
from rich.color import Color, ColorType
from rich.console import Console, Group, RenderableType
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from tests.support import chat_tui_pty
from toolang.base.types.message import (
    Message,
    Part,
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.base.types.model import (
    ModelRequest,
    Reasoning,
)
from toolang.base.types.policy import RunPolicy
from toolang.base.types.run import ModelCall, ToolCall
from toolang.cli.common.execution_progress import (
    ProgressBlock,
    ProgressRow,
    ProgressUpdate,
)
from toolang.cli.common.execution_progress.state import Metrics
from toolang.cli.common.script_progress.console import ProgressConsole
from toolang.cli.common.terminal_surfaces import (
    DARK_TERMINAL_SURFACES,
    TerminalSurfaces,
)
from toolang.cli.toolang.commands.chat import (
    blocks,
    events,
    rendering,
    shortcuts,
    slashes,
    tui,
    widgets,
)
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
    RunWorkdirUpdated,
    SteerReceipt,
    SteerError,
)
from toolang.cli.toolang.commands.chat.events import ChatUIEvent
from toolang.cli.toolang.commands.chat.input import QuickCommand
from toolang.cli.toolang.commands.chat.policy import (
    build_run_request,
    update_session_setting,
)
from toolang.cli.toolang.commands.chat.presenter import ChatRunPresenter
from toolang.common.errors import ToolangError
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
from toolang.execution.records import SteerControlPayload
from toolang.execution.schemas import (
    ControlInfo,
    RunControlRefData,
    RunDetail,
    RunnableRequest,
    RunRequest,
)
from toolang.execution.types import (
    value_for_type,
    Output,
    ControlRef,
    ErrorMessage,
    ErrorRef,
    FieldRef,
    ModelAccounting,
    ModelCost,
    ModelCostLine,
    ModelOverride,
    ModelStepGiven,
    ModelStepNoted,
    ModelUsageMeter,
    Occurrence,
    OccurrencePosition,
    RunOverride,
    SessionSetting,
    StepRef,
    ToolStepGiven,
)
from toolang.lang.ast import MapStmt, RunStmt, Span
from toolang.lang.input import CallInput

_CONTAINER_ID = "176191c1528b8e2861cc16422dee13ade59d4977c2148a9ebf5d36a06f090abb"
_HOST_DESCRIPTION = "macOS 27.0 arm64"
_HOST_SANDBOX_VALUE = f"host · {_HOST_DESCRIPTION}"


class _TerminalOutput(DummyOutput):
    columns = 100
    rows = 30

    def get_size(self) -> Size:
        return Size(rows=self.rows, columns=self.columns)


def _render_chat_layout(app: tui.ChatTuiApp) -> Screen:
    # Match Application._redraw: controls cache fragments for each render counter.
    app.app.render_counter += 1
    app.app.renderer.render(app.app, app.app.layout)
    screen = app.app.renderer.last_rendered_screen
    assert screen is not None
    return screen


@asynccontextmanager
async def _queue_test_app(
    *, progress_max_width: int = 120, inputbox_max_width: int | None = None
) -> AsyncIterator[tuple[tui.ChatTuiApp, _TerminalOutput]]:
    output = _TerminalOutput()
    with create_app_session(input=DummyInput(), output=output):
        app = tui.ChatTuiApp(
            thread_id="term_busy",
            setting=FakeClient().initial_setting(),
            input_history=None,
            client=FakeClient(),
            progress_max_width=progress_max_width,
            inputbox_max_width=inputbox_max_width,
        )
        app.active_run_id = "run_busy"
        app._set_status_running(True)
        for source in ("first", "second", "third"):
            app.handle_submit(source)
        with set_app(app.app):
            try:
                yield app, output
            finally:
                await app.app.cancel_and_wait_for_background_tasks()


def _screen_lines(screen: Screen, columns: int) -> list[str]:
    return [
        "".join(screen.data_buffer[row][col].char for col in range(columns))
        for row in range(screen.height)
    ]


def _cell_attrs(app: tui.ChatTuiApp, screen: Screen, row: int, col: int) -> Attrs:
    assert app.app.style is not None
    return app.app.style.get_attrs_for_style_str(screen.data_buffer[row][col].style)


def _parts(*parts: Part) -> Output:
    return Output(value_for_type("Part[]", tuple(parts)), "_")


def _output(step: StepRef) -> Output:
    return Output(
        value_for_type("Part[]", FieldRef.from_path(step, "output", "value")),
        "_",
    )


def _model_given(model: str = "test/model") -> ModelStepGiven:
    return ModelStepGiven(
        setup="test-setup", model=model, call=ModelCall(instructions="", messages=[])
    )


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


@pytest.mark.parametrize("status", ["succeeded", "failed", "canceled"])
def test_chat_run_lifecycle_keeps_control_bar_without_script_header(status) -> None:
    app = FakeApp()
    request = RunRequest(
        thread_id="term_1",
        request_id="review",
        runnable=RunnableRequest("agic:review", CallInput({"_": "Review this."})),
        model=ModelRequest("test/model", reasoning=Reasoning(effort="high")),
        policy=RunPolicy(),
    )
    control = blocks.RunControlBlock.create("Review this.", request=request)
    app.live_blocks.append(control)
    for event in (
        _run_begin(runnable_name="review"),
        _model_step_begin(),
        _model_step_end(output="Review output."),
        _run_end(status=status),
    ):
        events.handle_run_event(event, app)
        transcript = "".join(
            _render_text(block.render()) for block in (*app.finalized, *app.live_blocks)
        )
        assert "‣" not in transcript
        assert transcript.count("agic:review · test/model · high") == 1
        assert transcript.count("Review this.") == 1
    assert app.finished
    assert not app.live_blocks
    assert app.finalized[0] is control
    assert isinstance(app.finalized[-1], blocks.RunSummaryBlock)
    assert "Review output." in transcript
    assert f"run_1 {status}" in transcript


def test_chat_step_begin_finalizes_matching_steer_block() -> None:
    app = FakeApp()
    steer = blocks.RunSteerBlock.create(
        message="adjust",
        run_id="run_1",
    )
    events.handle_run_event(_run_begin(), app)
    app.presenter.add_steer("submission", steer, app)
    app.presenter.handle_steer_receipt(
        SteerReceipt("submission", "run_1", _steer_control(1)), app
    )
    events.handle_run_event(
        replace(_model_step_begin(), preceded_by=(ControlRef.for_run("run_1", 1),)), app
    )

    assert steer in app.finalized
    assert steer not in app.live_blocks


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
    assert f"{control_bottom}\n\n• Thinking" in transcript
    assert f"{control_bottom}\n\n\n• Thinking" not in transcript


def test_chat_uses_shared_progress_blocks_for_live_and_finalized_model_output() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(), app)
    events.handle_run_event(_model_step_begin(), app)
    assert [block.type for block in app.live_blocks] == [
        "ExecutionProgressBlock",
        "RunSummaryBlock",
    ]
    assert "• Thinking" in _render_text(app.live_blocks[0].render())

    events.handle_run_event(
        PartBegin(
            step=StepRef.parse("run_1.1"),
            part=0,
            part_type="text",
        ),
        app,
    )
    events.handle_run_event(
        PartDelta(
            step=StepRef.parse("run_1.1"),
            part=0,
            delta=TextDelta(text="drafting"),
        ),
        app,
    )
    streamed = _render_text(app.live_blocks[0].render())
    assert "• drafting" in streamed
    assert "Thinking" not in streamed
    events.handle_run_event(
        PartEnd(
            step=StepRef.parse("run_1.1"),
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
    assert "• drafting\n\n▪︎ run_1 succeeded" in transcript


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
            step=StepRef.parse("run_1.1"),
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
            noted=ModelStepNoted(
                accounting=ModelAccounting(input_tokens=12, output_tokens=3)
            ),
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
    assert "Thinking" not in rendered
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
    assert f"{control_bottom}\n\n[1] Map each item with summarize" in transcript
    assert f"{control_bottom}\n\n\n[1] Map each item with summarize" not in transcript
    assert "[1] Map each item with summarize, up to 2 at once\n\n• Mapped" in (
        transcript
    )
    assert "items\n\n▪︎ run_1 succeeded" in transcript
    assert "items\n\n\n▪︎ run_1 succeeded" not in transcript


def test_chat_moves_stable_markdown_to_scrollback_while_the_tail_stays_live() -> None:
    app = FakeApp()
    path = StepRef.parse("run_1.1")

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
    par_path = StepRef.parse("run_1.1")

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
                control=ControlRef.for_run(f"run_child_{item}", 0),
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
    assert "0 | #0 | • Thinking" in live
    assert "1 | #1 | • Thinking" in live

    events.handle_run_event(
        StepEnd(
            step=StepRef.parse("run_child_0.0"),
            kind="model",
            status="failed",
            error=ErrorMessage("model unavailable"),
        ),
        app,
    )
    events.handle_run_event(
        RunEnd(
            run="run_child_0",
            status="failed",
            error=ErrorRef(FieldRef.from_path(StepRef.parse("run_child_0.0"), "error")),
        ),
        app,
    )
    events.handle_run_event(
        StepEnd(
            step=StepRef.parse("run_child_1.0"),
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
            error=ErrorMessage("parallel step stopped because lane 0 (#0) failed"),
        ),
        app,
    )

    assert [block.type for block in app.live_blocks] == ["RunSummaryBlock"]
    finalized = _render_text(app.finalized[-1].render())
    assert "• Stopped · 1 failed · 1 canceled · 0/2 succeeded" in finalized
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

    assert f"{rendering.CONTROL_BAR_MARK} hello" in rendered
    assert "starting" not in rendered
    control_line = f"{rendering.CONTROL_BAR_MARK} hello" + " " * 13
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
    assert lines[1].startswith("▪︎ run_1 canceled  ")
    assert lines[1].endswith("3s")
    assert rendering.display_len(lines[1]) == 78
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
    assert lines[0].startswith("▪︎ run_1")
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
            step=StepRef.parse("run_1.0"),
            kind="model",
            status="succeeded",
            noted=ModelStepNoted(
                accounting=ModelAccounting(
                    input_tokens=3800,
                    output_tokens=120,
                    estimate=ModelCost(
                        amount=0.0,
                        currency="USD",
                        complete=True,
                        lines=(
                            ModelCostLine(
                                meter="input",
                                quantity=3800.0,
                                unit="token",
                                rate=0.0,
                                per=1000000.0,
                                amount=0.0,
                            ),
                            ModelCostLine(
                                meter="output",
                                quantity=120.0,
                                unit="token",
                                rate=0.0,
                                per=1000000.0,
                                amount=0.0,
                            ),
                        ),
                    ),
                    meters=(
                        ModelUsageMeter(
                            name="output.reasoning",
                            quantity=0.0,
                            unit="token",
                        ),
                    ),
                    selected="zero",
                )
            ),
        )
    )

    assert metrics.cost_known is True
    assert metrics.cost_approximate is False
    assert metrics.facts(include_runs=False) == [
        "1 model",
        "↑3.8k ↓120(0)",
    ]


@pytest.mark.parametrize(
    "accounting",
    [
        None,
        ModelAccounting(input_tokens=10, output_tokens=5),
        ModelAccounting(
            input_tokens=10,
            output_tokens=5,
            reported=ModelCost(amount=1.0, currency="EUR", complete=True),
            selected="reported",
        ),
    ],
)
def test_progress_marks_incomplete_usd_totals_as_approximate(
    accounting: ModelAccounting | None,
) -> None:
    metrics = Metrics()
    for index, item in enumerate(
        (
            ModelAccounting(
                input_tokens=10,
                output_tokens=5,
                reported=ModelCost(amount=0.03, currency="USD", complete=True),
                selected="reported",
            ),
            accounting,
        )
    ):
        metrics.record_step(
            StepEnd(
                step=StepRef.parse(f"run_1.{index}"),
                kind="model",
                status="succeeded",
                noted=ModelStepNoted(accounting=item),
            )
        )

    assert metrics.cost == 0.03
    assert metrics.cost_approximate is True
    assert "≈$0.03" in " ".join(metrics.facts())


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
        cost=float(amount),
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
                    quantity=150.0,
                    unit="token",
                ),
            ),
        ),
        ModelAccounting(input_tokens=100, output_tokens=20),
    ):
        metrics.record_step(
            StepEnd(
                step=StepRef.parse(f"run_1.{metrics.model_calls}"),
                kind="model",
                status="succeeded",
                noted=ModelStepNoted(accounting=accounting),
            )
        )

    assert metrics.facts(include_runs=False) == [
        "2 models",
        "↑1.1k ↓220(150+)",
    ]


@pytest.mark.parametrize("include_cost", [False, True])
def test_progress_groups_tokens_and_cost_in_one_fact(include_cost: bool) -> None:
    metrics = Metrics(
        model_calls=2,
        tool_calls=1,
        input_tokens=12200,
        output_tokens=528,
        cache_read_tokens=11248,
        reasoning_tokens=25,
        reasoning_known_calls=2,
        cost=0.0003,
        cost_known=True,
        cost_approximate=True,
    )
    usage = "↑12.2k(92.2%) ↓528(25)" + (" ≈$0.0003" if include_cost else "")

    assert metrics.facts(
        duration="12s", include_runs=False, include_cost=include_cost
    ) == ["12s", "2 models 1 tool", usage]


def test_chat_root_footer_keeps_short_facts_inline() -> None:
    block = blocks.RunSummaryBlock.create(_run_begin(run_id="run_pmqv7gfc"))
    block.update(_run_end(run_id="run_pmqv7gfc", status="succeeded"))

    lines = [line for line in _render_text(block.render()).splitlines() if line]
    assert len(lines) == 1
    assert lines[0].startswith("▪︎ run_pmqv7gfc succeeded  ")
    assert lines[0].endswith("3s")
    assert "succeeded ·" not in lines[0]
    assert rendering.display_len(lines[0]) == 78


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
    assert lines[0] == "▪︎ run_1 failed"
    assert all(line.startswith("  ") for line in lines[1:])
    assert "3s" in lines[1]
    assert "6 runs" in "\n".join(lines)
    assert all("─" not in line for line in lines)


def test_chat_tool_step_has_normal_marker_and_running_description() -> None:
    block = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_1.1",
            (ProgressRow("› Running a command", "active", surface="tool_summary"),),
        ),
        live=True,
        max_width=32,
    )
    segments = list(rendering.render_segments(block.render(), width=80))
    marker = next(segment for segment in segments if "›" in segment.text)
    content = next(segment for segment in segments if "Running" in segment.text)
    assert marker.style is None or not marker.style.dim
    assert content.style is None or not content.style.dim
    assert all(
        segment.style is None or segment.style.bgcolor is None for segment in segments
    )


@pytest.mark.parametrize("width", [16, 24, 80])
def test_chat_tool_failure_stays_two_lines_without_result_surfaces(width: int) -> None:
    block = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_1.1",
            (
                ProgressRow(
                    "› Failed to read a-very-long-document-name.md",
                    surface="tool_summary",
                ),
                ProgressRow(
                    "  " + "permission denied " * 8, "error", surface="tool_error"
                ),
            ),
        ),
        max_width=width,
    )
    segments = list(rendering.render_segments(block.render(), width=80))
    lines = [
        "".join(segment.text for segment in line)
        for line in Segment.split_lines(segments)
    ]
    # Chat terminates a committed block with a newline of its own.
    assert lines[-1] == ""
    lines.pop()
    assert len(lines) == 2
    assert all(len(line) <= width for line in lines)
    assert lines[0].startswith("› ")
    marker = next(segment for segment in segments if "›" in segment.text)
    assert marker.style is not None and marker.style.dim
    assert lines[1].startswith("  ")
    assert lines[1].endswith("…")
    assert all(
        segment.style is None or segment.style.bgcolor is None for segment in segments
    )
    error = next(segment for segment in segments if "permission" in segment.text)
    assert error.style is not None and error.style.color is not None
    assert error.style.color.name == "red"


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
                ProgressRow("1/3", leader="iteration"),
                ProgressRow(""),
                ProgressRow("[0] Run review"),
                ProgressRow(""),
            ),
        )
    )
    model = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_review.0",
            (ProgressRow("• Thinking", "active"),),
        ),
        live=True,
    )

    transcript = _render_text(header.render()) + _render_text(model.render())

    assert transcript.startswith("───")
    assert " 1/3 ─" in transcript
    assert "─\n\n[0] Run review" in transcript
    assert "[0] Run review\n\n• Thinking" in transcript
    assert "[0] Run review\n\n\n• Thinking" not in transcript


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
                ProgressRow("• Canceled · 1 canceled · 5/6 succeeded", "warning"),
                ProgressRow(
                    "  27.0s · 5 runs",
                    "progress",
                    right_text="run_1.2",
                ),
            ),
        )
    )

    rendered = _render_text(block.render())

    assert "• Canceled · 1 canceled · 5/6 succeeded" in rendered
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
    ("status", "caption_color"),
    [
        ("succeeded", None),
        ("failed", "red"),
        ("canceled", "yellow"),
    ],
)
def test_chat_run_footer_dims_marker_and_colors_caption(
    status: Literal["succeeded", "failed", "canceled"],
    caption_color: str | None,
) -> None:
    root_summary = blocks.RunSummaryBlock.create(_run_begin())
    root_summary.update(_run_end(status=status))
    segments = [
        segment
        for segment in rendering.render_segments(root_summary.render(), width=80)
        if segment.text.strip()
    ]

    assert _render_text(root_summary.render()).strip().startswith("▪︎ ")
    marker = next(segment for segment in segments if "▪︎" in segment.text)
    caption = next(segment for segment in segments if f"run_1 {status}" in segment.text)
    facts = next(segment for segment in segments if "3s" in segment.text)
    assert marker.style is not None
    assert marker.style.dim
    assert marker.style.color is None
    assert caption.style is not None
    assert not caption.style.bold
    if caption_color is None:
        assert caption.style.dim
        assert caption.style.color is None
    else:
        assert caption.style.color is not None
        assert caption.style.color.name == caption_color
    assert facts.style is not None
    assert facts.style.dim
    assert facts.style.color is None
    assert not facts.style.bold


def test_chat_command_blocks_render_run_and_steer_states() -> None:
    run_control = blocks.RunControlBlock.create("hello")
    run_control.update(_run_begin())
    run_text = _render_text(run_control.render())
    assert f"{rendering.CONTROL_BAR_MARK} hello" in run_text
    assert "run_1" not in run_text

    steer = blocks.RunSteerBlock.create(
        message="adjust",
        run_id="run_1",
        max_width=40,
    )
    steer_text = _render_text(steer.render())
    assert f"{rendering.CONTROL_BAR_MARK} adjust" in steer_text
    assert "+" not in steer_text
    assert "pending for next step" not in steer_text
    assert "run_1" not in steer_text
    assert not steer_text.splitlines()[0].strip()
    assert run_text.splitlines() == [
        " " * 80,
        f"{rendering.CONTROL_BAR_MARK} hello" + " " * 73,
        " " * 80,
    ]
    assert steer_text.splitlines() == [
        "",
        " " * 40,
        f"{rendering.CONTROL_BAR_MARK} adjust" + " " * 32,
        " " * 40,
    ]

    run_fragments = rendering.renderable_to_prompt_toolkit(run_control.render())
    steer_fragments = rendering.renderable_to_prompt_toolkit(steer.render())
    live_steer_lines = "".join(fragment[1] for fragment in steer_fragments).splitlines()
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
        if fragment[1] == rendering.CONTROL_BAR_MARK
    )
    steer_prompt_accent = rendering._prompt_toolkit_color(
        Color.parse(rendering.STEER_CONTROL_ACCENT)
    )
    steer_accent = next(
        fragment[0]
        for fragment in steer_fragments
        if fragment[1] == rendering.CONTROL_BAR_MARK
    )
    run_message = next(
        fragment[0] for fragment in run_fragments if "hello" in fragment[1]
    )
    steer_message = next(
        fragment[0] for fragment in steer_fragments if "adjust" in fragment[1]
    )

    assert rendering.RUN_CONTROL_ACCENT == "bright_cyan"
    assert rendering.STEER_CONTROL_ACCENT == "bright_magenta"
    assert rendering.QUICK_COMMAND_CONTROL_ACCENT == "yellow"
    input_background = DARK_TERMINAL_SURFACES.input_background
    assert run_accent == f"{run_prompt_accent} bg:{input_background} nodim"
    assert steer_accent == f"{steer_prompt_accent} bg:{input_background} nodim"
    assert f"bg:{DARK_TERMINAL_SURFACES.input_background}" in run_message
    assert f"bg:{DARK_TERMINAL_SURFACES.input_background}" in steer_message
    assert "nodim" in run_message.split()
    assert "nodim" in steer_message.split()
    assert "\x1b[22m" in stable_run
    assert [get_cwidth(line) for line in live_steer_lines[1:4]] == [40, 40, 40]

    steer.update(_model_step_begin(step_index=2))
    assert _render_text(steer.render()) == steer_text


@pytest.mark.parametrize(
    "block",
    [
        blocks.SlashBlock("/help", (), max_width=40),
        blocks.SlashResultBlock("/output", "run_1", (), max_width=40),
        blocks.SlashTableBlock(
            "/models",
            slashes.SlashTable("Found 0 models", ("MODEL",), ()),
            max_width=40,
        ),
        blocks.SlashHelpBlock(
            "/help",
            slashes.SlashHelp(
                (
                    slashes.SlashHelpSection(
                        "Commands",
                        (slashes.SlashHelpRow("/help", "", "Show help"),),
                    ),
                ),
                "Footer",
            ),
            max_width=40,
        ),
    ],
)
def test_chat_quick_command_control_bars_match_output_width(
    block: blocks.SlashBlock
    | blocks.SlashResultBlock
    | blocks.SlashTableBlock
    | blocks.SlashHelpBlock,
) -> None:
    lines = _render_text(block.render(), width=80).splitlines()

    assert [get_cwidth(line) for line in lines[:3]] == [40, 40, 40]


@pytest.mark.parametrize(
    ("block", "accent", "expected_accent_cells"),
    [
        (
            blocks.RunControlBlock.create("first\nsecond"),
            rendering.RUN_CONTROL_ACCENT,
            1,
        ),
        (
            blocks.RunSteerBlock.create(
                message="first\nsecond",
                run_id="run_1",
            ),
            rendering.STEER_CONTROL_ACCENT,
            1,
        ),
        (
            blocks.SlashBlock("first\nsecond", ()),
            rendering.QUICK_COMMAND_CONTROL_ACCENT,
            1,
        ),
    ],
)
def test_chat_two_line_control_bars_keep_both_padding_rows(
    block: blocks.MutableBlock | blocks.SlashBlock,
    accent: str,
    expected_accent_cells: int,
) -> None:
    segments = rendering.render_segments(block.render(), width=20)
    accent_cells = [
        segment
        for segment in segments
        if segment.text == rendering.CONTROL_BAR_MARK
        and segment.style is not None
        and segment.style.color is not None
        and segment.style.color.get_truecolor().hex
        == Color.parse(accent).get_truecolor().hex
        and segment.style.bgcolor is not None
        and segment.style.bgcolor.get_truecolor().hex
        == Color.parse(DARK_TERMINAL_SURFACES.input_background).get_truecolor().hex
    ]

    assert len(accent_cells) == expected_accent_cells
    rendered_lines = _render_text(block.render(), width=20).splitlines()
    body_lines = [line for line in rendered_lines if line[2:].strip()]
    assert [line[2:].rstrip() for line in body_lines] == ["first", "second"]


def test_chat_control_bar_keeps_padding_for_multiline_body() -> None:
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
        f"{rendering.CONTROL_BAR_MARK} first" + " " * 13,
        "  second" + " " * 12,
        " " * 20,
    ]
    assert three_lines == [
        " " * 20,
        f"{rendering.CONTROL_BAR_MARK} first" + " " * 13,
        "  second" + " " * 12,
        "  third" + " " * 13,
        " " * 20,
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
        if line[2:].strip()
    ]

    assert len(rendered_lines) == expected_rows
    assert rendered_lines[0].startswith(f"{rendering.CONTROL_BAR_MARK} ")
    assert all(line.startswith("  ") for line in rendered_lines[1:])
    assert all(rendering.display_len(line) == 20 for line in rendered_lines)
    assert "".join(line[2:].rstrip() for line in rendered_lines) == message


@pytest.mark.parametrize(
    "block",
    [
        blocks.RunSteerBlock.create(
            message="中文" * 20,
            run_id="run_1",
            max_width=20,
        ),
        blocks.SlashBlock("中文" * 20, (), max_width=20),
    ],
)
def test_chat_auxiliary_control_bars_wrap_wide_text_at_output_width(
    block: blocks.RunSteerBlock | blocks.SlashBlock,
) -> None:
    rendered_lines = [
        line
        for line in _render_text(block.render(), width=80).splitlines()
        if line[2:].strip()
    ]

    assert len(rendered_lines) == 5
    assert rendered_lines[0].startswith(f"{rendering.CONTROL_BAR_MARK} ")
    assert all(line.startswith("  ") for line in rendered_lines[1:])
    assert all(get_cwidth(line) == 20 for line in rendered_lines)
    assert "".join(line[2:].rstrip() for line in rendered_lines) == "中文" * 20


def test_chat_prompt_keeps_its_run_control_accent() -> None:
    prompt = widgets.PromptBox(lambda _event: None, lambda: None)

    container = prompt.container()

    assert isinstance(container, VSplit)
    accent, content = container.children
    assert isinstance(accent, Window)
    assert callable(accent.width) and cast(Callable[[], int], accent.width)() == 1
    assert accent.style == "class:control.run"
    assert accent.char == rendering.ACCENT_CELL
    assert widgets._chat_ui_palette()["control.run"] == "bg:ansibrightcyan"
    assert widgets._chat_ui_palette()["input"] == "bg:#1f1f1f"
    assert widgets._chat_ui_palette()["cursor"] == "reverse"
    assert widgets._chat_ui_palette()["input.cursor"] == "reverse"
    assert isinstance(content, HSplit)
    input_row = content.children[1]
    assert isinstance(input_row, VSplit)
    left_padding = input_row.children[0]
    assert isinstance(left_padding, Window)
    assert (
        callable(left_padding.width)
        and cast(Callable[[], int], left_padding.width)() == 1
    )
    input_window = input_row.children[1]
    assert isinstance(input_window, Window)
    assert isinstance(input_window.content, BufferControl)
    right_padding = input_row.children[2]
    assert isinstance(right_padding, Window)
    assert (
        callable(right_padding.width)
        and cast(Callable[[], int], right_padding.width)() == 2
    )
    assert right_padding.style == "class:input"
    assert right_padding.char == " "
    assert input_window.content.input_processors is not None
    placeholder = input_window.content.input_processors[0]
    assert isinstance(placeholder, ConditionalProcessor)
    assert isinstance(placeholder.processor, AfterInput)
    assert placeholder.processor.style == "class:input.placeholder"
    assert placeholder.processor.text == "Describe your task"
    assert placeholder.filter()
    assert widgets._chat_ui_palette()["input.placeholder"] == "bg:#1f1f1f dim"

    prompt.buffer.text = "hello"

    assert not placeholder.filter()


def test_chat_custom_surfaces_reach_input_queue_and_code_renderers() -> None:
    surfaces = TerminalSurfaces(
        input_background="#102030",
        queue_background="#203040",
        code_background="#304050",
        inline_code_background="#405060",
    )

    palette = widgets._chat_ui_palette(surfaces)
    assert palette["input"] == "bg:#102030"
    assert palette["queue"] == "bg:#203040"
    assert palette["queue.selected"] == "bg:#102030"
    assert palette["cursor"] == palette["input.cursor"] == "reverse"
    assert all("fg:" not in palette[name] for name in ("input", "queue"))

    controls = (
        blocks.RunControlBlock.create(
            "run input",
            input_background=surfaces.input_background,
        ),
        blocks.RunSteerBlock.create(
            message="steer input",
            run_id="run_1",
            input_background=surfaces.input_background,
        ),
        blocks.SlashBlock(
            "/help",
            (),
            input_background=surfaces.input_background,
        ),
    )
    for control, message in zip(
        controls,
        ("run input", "steer input", "/help"),
        strict=True,
    ):
        control_segment = next(
            segment
            for segment in rendering.render_segments(control.render())
            if message in segment.text
        )
        assert control_segment.style is not None
        assert control_segment.style.color is None
        assert control_segment.style.bgcolor is not None
        assert control_segment.style.bgcolor.get_truecolor().hex == "#102030"

    progress = blocks.ExecutionProgressBlock(
        ProgressBlock(
            "step:run_1.1",
            (ProgressRow("  result", "error", surface="tool_error"),),
        ),
        code_background=surfaces.code_background,
    )
    detail_segment = next(
        segment
        for segment in rendering.render_segments(progress.render())
        if "result" in segment.text
    )
    assert detail_segment.style is not None
    assert detail_segment.style.color is not None
    assert detail_segment.style.color.name == "red"
    assert detail_segment.style.bgcolor is None

    response = blocks.AssistantResponseBlock.from_parts(
        (TextPart("```text\nresult\n```"),),
        code_background=surfaces.code_background,
    )
    code_segments = [
        segment
        for segment in rendering.render_segments(response.render())
        if segment.style is not None and segment.style.bgcolor is not None
    ]
    assert code_segments
    assert {
        segment.style.bgcolor.get_truecolor().hex
        for segment in code_segments
        if segment.style is not None and segment.style.bgcolor is not None
    } == {"#304050"}
    assert any(
        segment.style is not None and segment.style.color is None
        for segment in code_segments
    )


def _assert_queue_summary(line: str, count: str, hint: str) -> None:
    width = get_cwidth(line)
    start = line.index(count)
    end = start + len(count)
    assert abs(start - (width - end)) <= 1
    assert not any(char in line for char in "()▸▾")
    if hint in line:
        assert line.endswith(f"{hint}  ")
        assert line[end : line.index(hint)].isspace()
        assert line.index(hint) - end >= 2
    else:
        assert line.strip() == count
        assert width - 2 - end < len(hint) + 2


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
    _assert_queue_summary(lines[0], "2 queued", "tab to focus")
    # Entries follow the summary; the last row separates Queue from Input.
    assert lines[1].startswith("  ↳ first")
    assert lines[1].index("↳") == lines[2].index("↳") == 2
    assert lines[2].strip() == "↳ second"
    assert lines[3].strip() == ""
    assert not any(
        action in "".join(lines)
        for action in ("collapse", "expand", "edit", "steer", "delete")
    )
    assert all(get_cwidth(line) == 100 for line in lines)
    assert not any("queue.selected" in style for style, _text in fragments)


@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize("terminal_width", [80, 82, 100, 101, 122, 160])
def test_chat_queue_panel_splits_selected_entry_actions_from_the_summary_hint(
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
    summary, selected, unselected, bottom = lines
    _assert_queue_summary(
        summary, "2 queued", "space to collapse" if focused else "tab to focus"
    )
    assert bottom.strip() == ""
    assert selected.startswith("  ↳ 任务 preview")
    assert unselected.startswith("  ↳ 任务 preview")
    if focused:
        hint = "m-enter steer · e edit · d delete  "
        assert selected.endswith(hint)
        assert selected[: selected.index(hint)].endswith("  ")
        assert selected[: selected.index(hint)].rstrip().endswith("…")
    else:
        assert selected[4:] == unselected[4:]
        assert selected.rstrip().endswith("…")
    assert not any(action in unselected for action in ("edit", "steer", "delete"))
    assert any("queue.selected" in style for style, _text in fragments) is focused
    assert "›" not in "".join(lines)
    assert all(get_cwidth(line) == terminal_width for line in lines)


@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize("terminal_width", [40, 100, 101, 160])
def test_chat_queue_panel_collapses_to_a_centered_count_and_right_hint(
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
    hint = "space to expand" if focused else "tab to focus"
    _assert_queue_summary(lines[0], "2 queued", hint)
    assert all(get_cwidth(line) == terminal_width for line in lines)
    assert not any(
        item in lines[0]
        for item in ("first", "second", "select", "edit", "delete", "steer")
    )


@pytest.mark.parametrize("terminal_width", [40, 100, 101])
def test_chat_queue_entry_hints_and_status_share_the_same_right_margin(
    monkeypatch: pytest.MonkeyPatch,
    terminal_width: int,
) -> None:
    panel = widgets.QueuePanel(lambda: ["first"])
    status = widgets.StatusBar("agic:chat", "model")
    monkeypatch.setattr(panel, "_terminal_width", lambda: terminal_width)
    monkeypatch.setattr(panel, "_has_focus", lambda: True)
    monkeypatch.setattr(status, "_terminal_width", lambda: terminal_width)

    # The last panel row is the trailing gap; the entry above it carries hints.
    entry_line = "".join(text for _style, text in panel._render()).splitlines()[-2]
    for running in (False, True):
        status.set_running(running)
        status_line = "".join(text for _style, text in status._render())
        assert get_cwidth(entry_line) == get_cwidth(status_line) == terminal_width
        assert get_cwidth(entry_line.rstrip()) == terminal_width - 2
        assert get_cwidth(status_line.rstrip()) == terminal_width - get_cwidth(
            widgets._STATUS_INSET
        )
        assert entry_line.endswith("  ")


@pytest.mark.parametrize("terminal_width", [12, 20, 25, 36, 40, 48, 49, 50, 80, 100])
def test_chat_queue_panel_keeps_the_count_hint_on_narrow_terminals(
    monkeypatch: pytest.MonkeyPatch,
    terminal_width: int,
) -> None:
    panel = widgets.QueuePanel(lambda: ["first"])
    monkeypatch.setattr(panel, "_terminal_width", lambda: terminal_width)
    monkeypatch.setattr(panel, "_has_focus", lambda: True)

    lines = "".join(text for _style, text in panel._render()).splitlines()

    assert len(lines) == panel.rows() == 3
    assert all(get_cwidth(line) == terminal_width for line in lines)
    assert lines[0].startswith("  ")
    _assert_queue_summary(lines[0], "1 queued", "space to collapse")
    assert lines[1].startswith("  ↳ ")
    assert lines[2].strip() == ""


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
    assert hint.endswith("  ")
    assert lines[1].endswith(hint)
    assert lines[1][: lines[1].index(hint)].endswith("  ")
    assert lines[1].startswith("  ↳ ")
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
    assert lines[1].startswith("  ↳ ")
    _assert_queue_summary(lines[0], "2 queued", "tab to focus")


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
        assert lines[1].startswith("  ↳")


@pytest.mark.parametrize("terminal_width", [10, 11, 12, 13])
@pytest.mark.parametrize("focused", [False, True])
def test_chat_queue_count_takes_priority_over_padding(
    monkeypatch: pytest.MonkeyPatch, terminal_width: int, focused: bool
) -> None:
    panel = widgets.QueuePanel(lambda: ["item"] * 10)
    panel.expanded = False
    monkeypatch.setattr(panel, "_terminal_width", lambda: terminal_width)
    monkeypatch.setattr(panel, "_has_focus", lambda: focused)

    line = "".join(text for _style, text in panel._render())

    assert line.strip() == "10 queued"
    assert line[0] == widgets.ACCENT_CELL
    assert get_cwidth(line) == terminal_width
    _assert_queue_summary(
        line, "10 queued", "space to expand" if focused else "tab to focus"
    )


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


@pytest.mark.parametrize("count", [0, 1, 8, 9])
def test_chat_queue_shows_at_most_eight_entries(count: int) -> None:
    panel = widgets.QueuePanel(lambda: [f"item {i}" for i in range(count)])
    lines = "".join(text for _style, text in panel._render()).splitlines()

    assert panel.rows() == (min(count, 8) + 2 if count else 0)
    assert len(lines[1 : 1 + min(count, 8)]) == min(count, 8)


def test_chat_queue_panel_uses_a_full_width_window_and_distinct_background() -> None:
    panel = widgets.QueuePanel(lambda: ["first"])

    container = panel.container()

    assert isinstance(container, ConditionalContainer)
    assert isinstance(container.content, Window)
    assert container.content.content is panel.view
    assert container.content.width == panel.width
    palette = widgets._chat_ui_palette()
    assert palette["queue"] == "bg:#121212"
    assert "queue.info" not in palette
    assert palette["queue.selected"] == "bg:#1f1f1f"
    assert palette["queue.icon"] == palette["queue.selected.icon"] == "dim"
    assert palette["queue.selected.hint"] == "dim"
    assert palette["queue.hint"] == "dim"
    assert palette["queue.accent"] == "bg:ansibrightmagenta"


def test_chat_queue_panel_focus_selects_and_windows_queued_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [f"queued input {index}" for index in range(1, 11)]
    panel = widgets.QueuePanel(lambda: items)
    monkeypatch.setattr(panel, "_terminal_width", lambda: 100)

    monkeypatch.setattr(panel, "_has_focus", lambda: True)
    for _ in range(9):
        assert panel.move_selection(1)

    fragments = panel._render()
    rendered = "".join(text for _style, text in fragments)
    lines = rendered.splitlines()

    assert widgets.MAX_QUEUE_ENTRIES == 8
    assert panel.rows() == 10
    assert panel.selected_index == 9
    _assert_queue_summary(lines[0], "10 queued", "space to collapse")
    assert "queued input 3" in lines[1]
    assert "queued input 10" in lines[8]
    assert lines[8].endswith("m-enter steer · e edit · d delete  ")
    assert "not shown" not in rendered
    assert all(get_cwidth(line) == 100 for line in lines)
    assert any(style == "class:queue.selected" for style, _text in fragments)


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


def test_chat_prompt_arrows_stay_out_of_input_history() -> None:
    prompt = widgets.PromptBox(lambda _event: None, lambda: None)
    keys = KeyBindings()
    prompt.bind(keys)
    prompt.history.append_string("previous input")

    def invoke(key: Keys) -> None:
        binding = next(item for item in keys.bindings if item.keys == (key,))
        cast(Any, binding.handler)(None)

    invoke(Keys.Up)
    invoke(Keys.Down)

    assert prompt.buffer.text == ""
    assert prompt.history_index is None

    prompt.buffer.text = "first\nsecond"
    prompt.buffer.cursor_position = len("first\nsecond")
    invoke(Keys.Up)

    assert prompt.buffer.document.cursor_position_row == 0
    assert prompt.buffer.text == "first\nsecond"

    invoke(Keys.ControlP)

    assert prompt.buffer.text == "previous input"
    assert shortcuts.PREVIOUS_HISTORY.bindings == (("c-p",),)
    assert shortcuts.NEXT_HISTORY.bindings == (("c-n",),)


def test_chat_prompt_bindings_cover_documented_shortcut_metadata() -> None:
    prompt = widgets.PromptBox(lambda _event: None, lambda: None)
    keys = KeyBindings()
    prompt.bind(keys)
    actual = {binding.keys for binding in keys.bindings}

    prompt_shortcuts = (
        *shortcuts.INPUT_SHORTCUTS,
        *(
            item
            for item in shortcuts.GLOBAL_SHORTCUTS
            if item is not shortcuts.SWITCH_AREA
        ),
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
        input_history=None,
        client=FakeClient(),
    )
    assert app.app.key_bindings is not None
    actual = {binding.keys for binding in app.app.key_bindings.bindings}

    for shortcut in (
        *shortcuts.INPUT_SHORTCUTS,
        *shortcuts.QUEUE_SHORTCUTS,
        *shortcuts.GLOBAL_SHORTCUTS,
    ):
        for raw_binding in (*shortcut.bindings, *shortcut.optional_bindings):
            expected = KeyBindings()
            try:
                expected.add(*raw_binding)(lambda _event: None)
            except ValueError:
                continue
            assert expected.bindings[0].keys in actual, shortcut.name


def test_chat_prompt_box_has_no_input_completion() -> None:
    prompt = widgets.PromptBox(lambda _event: None, lambda: None)
    assert (
        list(
            prompt.buffer.completer.get_completions(
                Document(":"), CompleteEvent(completion_requested=True)
            )
        )
        == []
    )
    assert not prompt.buffer.complete_while_typing()


def test_chat_prompt_grows_for_wrapped_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _TerminalOutput()
    output.columns = 10
    monkeypatch.setattr(
        "toolang.cli.common.input.get_app", lambda: SimpleNamespace(output=output)
    )
    prompt = widgets.PromptBox(lambda _event: None, lambda: None)

    prompt.buffer.text = "12345"
    assert prompt.rows() == 3

    prompt.buffer.text = "123456"
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
    assert f"  {'─' * 36}\n" in durable_text
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

    assert background_widths == [38, 38, 38, 38, 38]
    assert "".join(segment.text for segment in lines[1]).startswith("    x = 1")
    assert {
        segment.style.bgcolor.get_truecolor().hex
        for line in lines
        for segment in line
        if segment.style is not None and segment.style.bgcolor is not None
    } == {"#0b0b0b"}
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
    assert base_text.style.color is None
    assert number.style is not None
    assert number.style.color is not None
    assert number.style.color.number == 12


@pytest.mark.parametrize("background", ("#0b0b0b", "#f4f4f4", "#304050"))
@pytest.mark.parametrize("live", (False, True))
def test_chat_inline_code_uses_its_own_background(background: str, live: bool) -> None:
    markup = "before `value` after\n\n```text\nblock\n```"
    block = (
        blocks.ExecutionProgressBlock(
            ProgressBlock(
                "step:run_1.1",
                (ProgressRow(markup, "normal", format="markdown", prefix="• "),),
            ),
            live=True,
            code_background=background,
            inline_code_background="#405060",
        )
        if live
        else blocks.AssistantResponseBlock.from_parts(
            (TextPart(markup),),
            code_background=background,
            inline_code_background="#405060",
        )
    )
    segments = rendering.render_segments(block.render())
    code = next(segment for segment in segments if segment.text == "value")
    fenced = next(segment for segment in segments if "block" in segment.text)

    assert code.style is not None
    assert code.style == Console().get_style("markdown.code") + Style(bgcolor="#405060")
    assert fenced.style is not None
    assert fenced.style.bgcolor == Color.parse(background)
    assert all(
        segment.style is None or segment.style.bgcolor is None
        for segment in segments
        if "before" in segment.text or "after" in segment.text
    )


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


def test_chat_markdown_uses_only_code_surface_truecolor() -> None:
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

    assert all("fg:#" not in style for style in styles)
    assert any("bg:#0b0b0b" in style for style in styles)
    assert "\x1b[38;2" not in stable
    assert "\x1b[48;2;11;11;11m" in stable


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


@pytest.mark.parametrize("expanded", [False, True])
def test_chat_queue_removal_leaves_live_space_that_new_output_consumes(
    monkeypatch: pytest.MonkeyPatch, expanded: bool
) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, output):
            app.queue_panel.expanded = expanded
            app.prompt.replace_input("keep draft")
            writes: list[Sequence[RenderableType | None]] = []
            monkeypatch.setattr(app, "_write_scrollback", writes.append)
            monkeypatch.setattr(
                rendering,
                "write_renderables",
                lambda values, **_kwargs: writes.append(values),
            )

            def input_row() -> int:
                lines = _screen_lines(_render_chat_layout(app), output.columns)
                return next(i for i, line in enumerate(lines) if "keep draft" in line)

            original_row = input_row()
            original_queue_rows = app.queue_panel.rows()
            while app.queue:
                app._pop_queued_call(0)
                app._commit_ui_update()
                assert input_row() == original_row
                assert app._input_spacer_rows() == (
                    original_queue_rows - app.queue_panel.rows()
                )
                assert not app._pending_scrollback
                assert not writes

            for count in range(1, original_queue_rows + 1):
                app.unfinalized_blocks[:] = [
                    blocks.ExecutionProgressBlock(
                        ProgressBlock(
                            "step:run_busy.0",
                            tuple(ProgressRow(f"live line {i}") for i in range(count)),
                        )
                    )
                ]
                app._commit_ui_update()
                assert input_row() == original_row
                assert app._input_spacer_rows() == original_queue_rows - count
                assert not writes
            assert app.prompt.buffer.text == "keep draft"

    asyncio.run(exercise())


@pytest.mark.parametrize("columns", [40, 82, 100, 101, 160])
@pytest.mark.parametrize("expanded", [False, True])
@pytest.mark.parametrize("focused", [False, True])
def test_chat_queue_layout_centers_count_and_joins_input(
    columns: int,
    expanded: bool,
    focused: bool,
) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, output):
            output.columns = columns
            app.prompt.buffer.text = "Keep typing"
            app.queue_panel.expanded = expanded
            app._footer_row_floor = 20
            if focused:
                app.app.layout.focus(app.queue_panel.view)

            screen = _render_chat_layout(app)
            width = min(columns, app.progress_max_width)
            lines = _screen_lines(screen, width)
            summary_row = next(i for i, line in enumerate(lines) if "3 queued" in line)
            input_row = next(i for i, line in enumerate(lines) if "Keep typing" in line)
            panel_rows = 5 if expanded else 1
            panel_bottom = summary_row + panel_rows - 1

            assert app.queue_panel.width() == width
            assert app.queue_panel.rows() == panel_rows
            assert input_row == panel_bottom + 2
            assert not lines[input_row - 1].strip()
            assert app._input_spacer_rows() > 0
            assert app._available_live_rows() == 30 - panel_rows - app.prompt.rows() - 3
            assert get_cwidth(lines[panel_bottom]) == width
            assert get_cwidth(lines[input_row + 2].rstrip()) == width - get_cwidth(
                widgets._STATUS_INSET
            )
            if expanded:
                assert lines[panel_bottom].strip() == ""
                _assert_queue_summary(
                    lines[summary_row],
                    "3 queued",
                    "space to collapse" if focused else "tab to focus",
                )
                assert lines[panel_bottom - 1].startswith("  ↳")
            else:
                hint = "space to expand" if focused else "tab to focus"
                _assert_queue_summary(lines[summary_row], "3 queued", hint)

    asyncio.run(exercise())


@pytest.mark.parametrize("selected_index", [0, 1, 2])
@pytest.mark.parametrize("expanded", [False, True])
@pytest.mark.parametrize("focused", [False, True])
def test_chat_queue_focus_styles_respect_selection_padding(
    selected_index: int, expanded: bool, focused: bool
) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, output):
            app.queue_panel.move_selection(selected_index)
            app.queue_panel.expanded = expanded
            if focused:
                app.app.layout.focus(app.queue_panel.view)
            screen = _render_chat_layout(app)
            lines = _screen_lines(screen, output.columns)
            top = next(i for i, line in enumerate(lines) if "3 queued" in line)
            bottom = top + (4 if expanded else 0)

            summary = _cell_attrs(app, screen, top, lines[top].index("3 queued"))
            assert summary.dim is not focused
            assert summary.color == ""
            assert not summary.bold
            for row in range(top, bottom + 1):
                assert _cell_attrs(app, screen, row, 0).bgcolor == "ansibrightmagenta"
                selected = expanded and focused and row == top + 1 + selected_index
                background = "1f1f1f" if selected else "121212"
                assert all(
                    _cell_attrs(app, screen, row, col).bgcolor == background
                    for col in range(1, output.columns)
                )
                if expanded and top < row < bottom:
                    icon = _cell_attrs(app, screen, row, 2)
                    body = _cell_attrs(app, screen, row, 6)
                    assert icon.dim and not body.dim
                    assert not icon.bold and not body.bold
                    assert icon.color == body.color == ""
                if selected:
                    assert lines[row].index("↳") == 2
                    assert lines[row].endswith("m-enter steer · e edit · d delete  ")
                    hints = _cell_attrs(app, screen, row, output.columns - 3)
                    assert hints.dim and hints.color == ""
            summary_hint = _cell_attrs(
                app, screen, top, lines[top].index("space" if focused else "tab")
            )
            assert summary_hint.dim and summary_hint.color == ""
            assert _cell_attrs(app, screen, bottom + 1, 0).bgcolor == "ansibrightcyan"
            assert _cell_attrs(app, screen, bottom + 2, 0).bgcolor == "ansibrightcyan"
            assert _cell_attrs(app, screen, bottom + 1, 1).bgcolor == "1f1f1f"
            if not focused:
                assert not any(
                    "m-enter steer" in line for line in lines[top : bottom + 1]
                )

    asyncio.run(exercise())


@pytest.mark.parametrize("terminal_rows", [8, 12, 30])
@pytest.mark.parametrize("columns", [25, 100])
def test_chat_queue_eight_entry_limit_adapts_to_available_height(
    terminal_rows: int, columns: int
) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, output):
            output.rows = terminal_rows
            output.columns = columns
            for number in range(4, 11):
                app.handle_submit(f"item {number}")
            app.queue_panel.move_selection(9)
            app.app.layout.focus(app.queue_panel.view)
            screen = _render_chat_layout(app)
            lines = _screen_lines(screen, output.columns)
            assert not any("Window too small" in line for line in lines)
            top = next(i for i, line in enumerate(lines) if "10 queued" in line)
            entry_count = min(8, max(1, terminal_rows - 8))
            entry_rows = lines[top + 1 : top + 1 + entry_count]
            assert len(entry_rows) == entry_count
            assert all(widgets._QUEUE_ENTRY_ICON in row for row in entry_rows)
            if columns == 100:
                previews = [
                    "first",
                    "second",
                    "third",
                    *(f"item {number}" for number in range(4, 11)),
                ]
                assert previews[10 - entry_count] in lines[top + 1]
                assert "item 10" in lines[top + entry_count]
            panel_bottom = top + app.queue_panel.rows() - 1
            assert "Describe your task"[: columns - 5] in lines[panel_bottom + 2]
            assert lines[panel_bottom + 4].startswith(f"{widgets._STATUS_INSET}agic")

    asyncio.run(exercise())


@pytest.mark.parametrize("columns", [25, 100])
@pytest.mark.parametrize("expanded", [False, True])
@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize(
    "draft",
    ["\n".join(["draft"] * 11 + ["last"]), "x" * 800 + "last"],
    ids=["multiline", "wrapped"],
)
def test_chat_queue_reserves_space_for_a_scrolling_draft_after_resize(
    monkeypatch: pytest.MonkeyPatch,
    columns: int,
    expanded: bool,
    focused: bool,
    draft: str,
) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, output):
            output.columns = columns
            monkeypatch.setenv("COLUMNS", str(columns))
            app.prompt.replace_input(draft)
            app.queue_panel.move_selection(2)
            app.queue_panel.expanded = expanded
            if focused:
                app.app.layout.focus(app.queue_panel.view)

            for terminal_rows in (30, 12, 8, 30):
                output.rows = terminal_rows
                screen = _render_chat_layout(app)
                lines = _screen_lines(screen, columns)
                assert not any("Window too small" in line for line in lines)
                assert any("3 queued" in line for line in lines)
                assert any("last" in line for line in lines)
                assert lines[-1].startswith(f"{widgets._STATUS_INSET}agic")
                assert app.prompt.rows() + app.queue_panel.rows() + 1 <= terminal_rows
                assert app.prompt.buffer.text == draft
                assert app.prompt.buffer.cursor_position == len(draft)
                assert screen.show_cursor is not focused
                if expanded:
                    assert any(widgets._QUEUE_ENTRY_ICON in line for line in lines)
                if not focused:
                    cursor = screen.get_cursor_position(app.app.layout.current_window)
                    assert 0 <= cursor.y < screen.height - 2

    asyncio.run(exercise())


@pytest.mark.parametrize("commits", [1, 3])
@pytest.mark.parametrize("timeout", [False, True])
def test_chat_delayed_cursor_reports_do_not_scroll_unused_terminal_rows(
    commits: int,
    timeout: bool,
) -> None:
    class ReportingOutput(_TerminalOutput):
        cursor_row = 0
        scrolled_rows = 0

        def get_rows_below_cursor_position(self) -> int:
            raise NotImplementedError

        def write(self, data: str) -> None:
            for char in data:
                if char == "\n":
                    self.cursor_row += 1
                    if self.cursor_row >= self.rows:
                        self.cursor_row = self.rows - 1
                        self.scrolled_rows += 1

        def write_raw(self, data: str) -> None:
            self.write(data)

        def cursor_up(self, amount: int) -> None:
            self.cursor_row = max(0, self.cursor_row - amount)

    async def exercise() -> None:
        output = ReportingOutput()
        with create_app_session(input=DummyInput(), output=output):
            app = tui.ChatTuiApp(
                thread_id=None,
                setting=FakeClient().initial_setting(),
                input_history=None,
                client=FakeClient(),
            )
            with set_app(app.app):
                renderer = app.app.renderer
                renderer.cpr_support = CPR_Support.SUPPORTED
                reports = []
                for index in range(commits + 1):
                    if index:
                        if timeout:
                            await renderer.wait_for_cpr_responses(timeout=0)
                        renderer.erase(leave_alternate_screen=False)
                        app._write_scrollback([Text("stable line\n" * 5)])
                    origin_row = output.cursor_row + 1
                    pending_count = len(renderer._waiting_for_cpr_futures)
                    renderer.request_absolute_cursor_position()
                    if len(renderer._waiting_for_cpr_futures) > pending_count:
                        reports.append(origin_row)
                    _render_chat_layout(app)

                current_request = (
                    renderer._waiting_for_cpr_futures[-1]
                    if renderer._waiting_for_cpr_futures
                    else None
                )
                for index, row in enumerate(reports):
                    renderer.report_absolute_cursor_row(row)
                    if current_request is not None:
                        assert current_request.done() == (index == len(reports) - 1)
                    screen = _render_chat_layout(app)
                    lines = _screen_lines(screen, output.columns)
                    assert output.scrolled_rows == 0
                    assert any("Describe your task" in line for line in lines)

                if current_request is None:
                    renderer.erase()
                    renderer.request_absolute_cursor_position()
                    renderer.report_absolute_cursor_row(origin_row)
                    _render_chat_layout(app)
                assert output.scrolled_rows == 0
                assert not renderer.waiting_for_cpr
                assert renderer._min_available_height == output.rows - origin_row + 1
                await app.app.cancel_and_wait_for_background_tasks()

    asyncio.run(exercise())


@pytest.mark.parametrize("report_before_signal", [False, True])
def test_resize_notification_after_refresh_preserves_cursor_report(
    monkeypatch, report_before_signal
):
    async def exercise():
        async with _queue_test_app() as (app, output):
            app.queue.clear()
            app._finish_active_run()
            _render_chat_layout(app)
            renderer = app.app.renderer
            assert isinstance(renderer, tui._ChatRenderer)
            renderer.cpr_support = CPR_Support.SUPPORTED
            queries = []

            def unavailable():
                raise NotImplementedError

            monkeypatch.setattr(output, "get_rows_below_cursor_position", unavailable)
            monkeypatch.setattr(output, "ask_for_cpr", lambda: queries.append(True))
            monkeypatch.setattr(app.app, "_redraw", lambda: _render_chat_layout(app))
            output.columns = 40
            _render_chat_layout(app)
            if report_before_signal:
                renderer.report_absolute_cursor_row(18)
                _render_chat_layout(app)
            app.app._on_resize()
            if not report_before_signal:
                renderer.report_absolute_cursor_row(18)
                _render_chat_layout(app)

            assert queries == [True]
            assert not renderer.waiting_for_cpr
            assert renderer._min_available_height > 0
            assert renderer._resize_bottom_gap is None

    asyncio.run(exercise())


@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize("support", [CPR_Support.UNKNOWN, CPR_Support.SUPPORTED])
def test_chat_renderer_ignores_obsolete_cursor_reports(
    monkeypatch: pytest.MonkeyPatch, timeout: bool, support: CPR_Support
) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, output):
            app.queue.clear()
            app._finish_active_run()
            renderer = app.app.renderer
            renderer.cpr_support = support

            # DummyOutput has a synchronous cursor API; use VT100's async path.
            def unavailable() -> int:
                raise NotImplementedError

            monkeypatch.setattr(output, "get_rows_below_cursor_position", unavailable)
            renderer.request_absolute_cursor_position()
            old_request = renderer._waiting_for_cpr_futures[0]
            if timeout:
                await renderer.wait_for_cpr_responses(timeout=0)
            else:
                renderer.erase()
            renderer.report_absolute_cursor_row(1)
            assert old_request.done()
            assert renderer._min_available_height == 0
            renderer.request_absolute_cursor_position()
            renderer.report_absolute_cursor_row(8)
            assert renderer._min_available_height == output.rows - 7
            assert not renderer.waiting_for_cpr

    asyncio.run(exercise())


@pytest.mark.parametrize("queries", [1, 3])
def test_chat_pauses_cursor_queries_until_timed_out_replies_are_drained(
    monkeypatch: pytest.MonkeyPatch, queries: int
) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, output):
            renderer = app.app.renderer
            renderer.cpr_support = CPR_Support.SUPPORTED
            sent: list[bool] = []

            def unavailable() -> int:
                raise NotImplementedError

            monkeypatch.setattr(output, "get_rows_below_cursor_position", unavailable)
            monkeypatch.setattr(output, "ask_for_cpr", lambda: sent.append(True))
            for _ in range(queries):
                renderer.request_absolute_cursor_position()
            await renderer.wait_for_cpr_responses(timeout=0)

            # A missing reply must not consume newer replies indefinitely or
            # cause each subsequent terminal transaction to wait for a timeout.
            for _ in range(3):
                renderer.erase()
                renderer.request_absolute_cursor_position()
                assert len(sent) == queries
                assert not renderer.waiting_for_cpr
                await renderer.wait_for_cpr_responses(timeout=0)

            for index in range(queries):
                renderer.report_absolute_cursor_row(1)
                assert renderer._min_available_height == 0
                renderer.request_absolute_cursor_position()
                drained = index == queries - 1
                assert len(sent) == queries + int(drained)
                assert renderer.waiting_for_cpr == drained

            renderer.report_absolute_cursor_row(8)
            assert renderer._min_available_height == output.rows - 7
            assert not renderer.waiting_for_cpr

    asyncio.run(exercise())


def test_chat_input_reclaims_height_when_queue_empties() -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, output):
            output.rows = 8
            draft = "\n".join(["draft"] * 11 + ["last"])
            app.prompt.replace_input(draft)
            app.app.layout.focus(app.queue_panel.view)
            _render_chat_layout(app)
            assert app.prompt.rows() == 3

            while app.queue:
                app._pop_queued_call(0)
            screen = _render_chat_layout(app)
            lines = _screen_lines(screen, output.columns)
            assert app.prompt.rows() == 5
            assert app.queue_panel.rows() == 0
            assert any("last" in line for line in lines)
            assert "agic:chat" in lines[-1]
            assert app.prompt.buffer.text == draft
            assert screen.show_cursor

    asyncio.run(exercise())


@pytest.mark.parametrize("max_width", [60, 120])
@pytest.mark.parametrize("inputbox_max_width", [None, 40, 160])
@pytest.mark.parametrize("expanded", [False, True])
def test_chat_input_area_uses_its_own_width_after_resize(
    monkeypatch: pytest.MonkeyPatch,
    expanded: bool,
    max_width: int,
    inputbox_max_width: int | None,
) -> None:
    async def exercise() -> None:
        async with _queue_test_app(
            progress_max_width=max_width, inputbox_max_width=inputbox_max_width
        ) as (app, output):
            # An exported shell size can differ from the actual terminal output.
            monkeypatch.setenv("COLUMNS", "120")
            app.prompt.replace_input("x" * 79)
            app.queue_panel.expanded = expanded

            for columns in (40, 82, 100, 160):
                output.columns = columns
                width = min(columns, inputbox_max_width or max_width)
                screen = _render_chat_layout(app)
                lines = _screen_lines(screen, columns)
                input_width = width - 4
                assert app.prompt.rows() == (79 + input_width) // input_width + 2
                assert app.queue_panel.width() == width
                assert lines[-1][:width].endswith(
                    f"openai/gpt-5{widgets._STATUS_INSET}"
                )
                assert get_cwidth(lines[-1].rstrip()) == width - get_cwidth(
                    widgets._STATUS_INSET
                )
                assert app.app.style is not None
                for row in range(screen.height):
                    for column in range(width, columns):
                        cell = screen.data_buffer[row][column]
                        assert cell.char == " "
                        assert not app.app.style.get_attrs_for_style_str(
                            cell.style
                        ).bgcolor

    asyncio.run(exercise())


def test_chat_resize_during_layout_uses_a_consistent_frame_size(monkeypatch):
    async def exercise():
        async with _queue_test_app() as (app, output):
            app.prompt.replace_input("draft " * 12)
            reads = []

            def changing_size():
                size = Size(rows=12, columns=80 if len(reads) % 2 == 0 else 81)
                reads.append(size)
                return size

            monkeypatch.setattr(output, "get_size", changing_size)
            for columns in (80, 81, 80):
                screen = _render_chat_layout(app)
                lines = _screen_lines(screen, columns)
                assert not any("Window too small" in line for line in lines)
                assert any("3 queued" in line for line in lines)
                assert app.app.renderer._last_size == Size(rows=12, columns=columns)
            assert len(reads) == 3
            assert app.prompt.buffer.text == "draft " * 12

    asyncio.run(exercise())


@pytest.mark.parametrize("max_width", [60, 120])
@pytest.mark.parametrize("inputbox_max_width", [40, 160])
def test_chat_live_and_committed_controls_keep_output_width(
    monkeypatch, max_width, inputbox_max_width
):
    async def exercise():
        async with _queue_test_app(
            progress_max_width=max_width, inputbox_max_width=inputbox_max_width
        ) as (app, output):
            monkeypatch.setenv("COLUMNS", "200")
            block = blocks.RunControlBlock.create("submitted words " * 18)
            app.unfinalized_blocks = [block]
            written = []
            monkeypatch.setattr(output, "write_raw", written.append)
            for columns in (200, 80, 40):
                output.columns = columns
                width = min(columns, max_width)
                app.app.render_counter += 1
                live = fragment_list_to_text(app._live_fragments()).splitlines()
                written.clear()
                app._write_scrollback([block])
                committed = Text.from_ansi("".join(written)).plain.splitlines()
                assert live == committed
                assert max(map(get_cwidth, live)) == width
                assert all(get_cwidth(line) <= width for line in live)
                assert app._live_area_height() == len(live)

    asyncio.run(exercise())


@pytest.mark.parametrize("draft", ["", "draft " * 12, "中文" * 30])
@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("columns", [25, 40, 100, 160])
@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("refresh", ["resize", "render", "erase"])
def test_chat_resize_erases_the_reflowed_live_origin(
    monkeypatch: pytest.MonkeyPatch,
    draft: str,
    queued: bool,
    columns: int,
    live: bool,
    refresh: str,
) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, output):
            if not queued:
                app.queue.clear()
            if live:
                app.unfinalized_blocks.append(
                    blocks.ExecutionProgressBlock(
                        ProgressBlock("step:run_1.0", (ProgressRow("working"),))
                    )
                )
            app.prompt.replace_input(draft)
            screen = _render_chat_layout(app)
            cursor = screen.get_cursor_position(app.app.layout.current_window)

            # Background padding uses erase-character, so only actual text
            # contributes to terminal reflow before SIGWINCH.
            def text_row(y: int, end: int) -> str:
                return "".join(
                    screen.data_buffer[y][x].char for x in range(end)
                ).rstrip()

            reflowed_y = sum(
                max(1, len(chop_cells(text_row(y, 100), columns)))
                for y in range(cursor.y)
            )
            reflowed_y += max(
                0, len(chop_cells(text_row(cursor.y, cursor.x + 1), columns)) - 1
            )
            physical_cursor = [cursor.x % columns, reflowed_y]
            erased_from: list[tuple[int, int]] = []

            def backward(amount: int) -> None:
                physical_cursor[0] = max(0, physical_cursor[0] - amount)

            def up(amount: int) -> None:
                physical_cursor[1] -= amount

            def write(value: str) -> None:
                # Rendering can return to column zero with a carriage return.
                if "\r" in value:
                    physical_cursor[0] = 0

            monkeypatch.setattr(output, "cursor_backward", backward)
            monkeypatch.setattr(output, "cursor_up", up)
            monkeypatch.setattr(output, "write", write)
            monkeypatch.setattr(
                output,
                "erase_down",
                lambda: erased_from.append((physical_cursor[0], physical_cursor[1])),
            )
            output.columns = columns
            if refresh == "resize":
                monkeypatch.setattr(
                    app.app, "_redraw", lambda: _render_chat_layout(app)
                )
                app.app._on_resize()
            elif refresh == "render":
                _render_chat_layout(app)
            else:
                app.app.renderer.erase(leave_alternate_screen=False)

            if columns == 100 and refresh != "erase":
                assert erased_from == []
            else:
                assert erased_from[0] == (0, 0)
            assert app.prompt.buffer.text == draft
            assert app.prompt.buffer.cursor_position == len(draft)

    asyncio.run(exercise())


@pytest.mark.parametrize("selected_index", [0, 1, 2])
def test_chat_queue_preserves_cursor_and_selection_across_transitions(
    selected_index: int,
) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, output):
            app.queue_panel.move_selection(selected_index)
            app.prompt.buffer.text = "Keep typing"
            app.prompt.buffer.cursor_position = 4
            for columns in (160, 40, 101):
                output.columns = columns
                for expanded in (True, False, True):
                    app.queue_panel.expanded = expanded
                    for focused in (False, True, False):
                        app.app.layout.focus(
                            app.queue_panel.view if focused else app.prompt.buffer
                        )
                        screen = _render_chat_layout(app)
                        assert app.queue_panel.selected_index == selected_index
                        assert app.prompt.buffer.cursor_position == 4
                        assert screen.show_cursor is not focused
                        if not focused:
                            lines = _screen_lines(screen, columns)
                            input_row = next(
                                i
                                for i, line in enumerate(lines)
                                if "Keep typing" in line
                            )
                            cursor = screen.get_cursor_position(
                                app.app.layout.current_window
                            )
                            assert (cursor.x, cursor.y) == (6, input_row)

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

    marker = next(segment for segment in segments if "•" in segment.text)
    assert marker.style is None or not marker.style.dim
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

    assert not rendered_lines[0].strip()
    assert rendered_lines[1].startswith(f"{rendering.CONTROL_BAR_MARK} /?")
    assert not rendered_lines[2].strip()
    assert "  Slash commands act immediately." in rendered
    assert "/model [MODEL]" in rendered
    assert "List or switch models." in rendered
    assert not rendered.endswith("\n")
    command = next(segment for segment in segments if segment.text == "/model")
    argument = next(segment for segment in segments if segment.text == "[MODEL]")
    quick_accent_hex = (
        Color.parse(rendering.QUICK_COMMAND_CONTROL_ACCENT).get_truecolor().hex
    )
    input_background_hex = (
        Color.parse(DARK_TERMINAL_SURFACES.input_background).get_truecolor().hex
    )
    quick_accents = [
        segment
        for segment in all_segments
        if segment.text == rendering.CONTROL_BAR_MARK
        and segment.style is not None
        and segment.style.color is not None
        and segment.style.color.get_truecolor().hex == quick_accent_hex
    ]

    assert len(quick_accents) == 1
    assert all(
        segment.style is not None
        and segment.style.color is not None
        and segment.style.color.get_truecolor().hex == quick_accent_hex
        and segment.style.bgcolor is not None
        and segment.style.bgcolor.get_truecolor().hex == input_background_hex
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
    heading = next(segment for segment in segments if segment.text.strip() == "Session")
    command = next(segment for segment in segments if segment.text == "/model")
    argument = next(segment for segment in segments if segment.text == "[MODEL]")
    alias = next(segment for segment in segments if "(alias: /show)" in segment.text)
    narrow = _render_text(
        blocks.SlashHelpBlock("/help", outcome.content, max_width=24).render(),
        width=100,
    )
    narrow_words = " ".join(narrow.split())

    assert all(get_cwidth(line) <= 60 for line in rendered.splitlines())
    assert all(get_cwidth(line) <= 24 for line in narrow.splitlines())
    assert "Set model or parameters" in narrow_words
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


def test_chat_header_leaves_two_empty_rows_before_initial_input_or_control() -> None:
    header = blocks.HeaderBlock(
        client_version="0.4.0-client", executor_metadata=FakeClient().executor_metadata
    )
    output = rendering.renderables_output([header.render()], width=120)
    assert len(output) - len(output.rstrip("\n")) == 3


def test_chat_header_uses_wide_runtime_layout() -> None:
    block = blocks.HeaderBlock(
        client_version="0.4.0-client",
        executor_metadata=ChatExecutorMetadata(
            sandbox_driver="host",
            sandbox_detail=_HOST_DESCRIPTION,
            endpoint="http://localhost:7001",
            version="0.3.9",
            workspaces=("lab", "toolang"),
        ),
    )
    rendered = _render_text(block.render(), width=100)
    lines = rendered.splitlines()
    assert lines[0] == ""
    assert "Chat v0.4.0-client" in lines[1]
    assert "Toolang Chat" not in rendered
    assert rendered.count("v0.3.9") == 1
    assert "http://localhost:7001" not in rendered
    assert "home" not in rendered
    assert "executor" not in rendered
    assert "embedded" not in rendered
    runtime_line, sandbox_line, workspace_line = (
        next(line for line in lines if key in line)
        for key in ("runtime", "sandbox", "workspaces")
    )
    assert "████" in runtime_line
    assert "⬤" in sandbox_line
    assert "███" in workspace_line
    assert runtime_line.split("runtime", 1)[1].strip("│ ") == "v0.3.9"
    assert _HOST_SANDBOX_VALUE in sandbox_line
    assert "lab, toolang" in workspace_line
    assert runtime_line.index("runtime") == sandbox_line.index("sandbox")
    assert runtime_line.index("runtime") == workspace_line.index("workspaces")
    assert runtime_line.index("v0.3.9") == sandbox_line.index("host")
    assert runtime_line.index("v0.3.9") == workspace_line.index("lab")
    bordered = [line for line in lines if line]
    assert len({len(line) for line in bordered}) == 1
    assert not bordered[1].strip("│ ")
    assert not bordered[-2].strip("│ ")


@pytest.mark.parametrize("width", [30, 40, 60])
def test_chat_header_stacks_without_clipping_in_a_narrow_terminal(width: int) -> None:
    rendered = _render_text(
        blocks.HeaderBlock(
            client_version="0.4.0-client",
            executor_metadata=ChatExecutorMetadata(
                sandbox_driver="docker",
                sandbox_detail="registry.example:5000/team/python:3.13-slim",
                endpoint="http://runtime.test:7001",
                version="v0.3.9",
                workspaces=("lab", "toolang-with-a-long-workspace-name"),
            ),
        ).render(),
        width=width,
    )
    lines = rendered.splitlines()
    logo_index = next(index for index, line in enumerate(lines) if "⬤" in line)
    runtime_index = next(index for index, line in enumerate(lines) if "runtime" in line)
    assert runtime_index > logo_index + 1
    assert all(len(line) <= width for line in lines)
    bordered = [line for line in lines if line]
    assert len({len(line) for line in bordered}) == 1
    assert not bordered[1].strip("│ ")
    assert not bordered[-2].strip("│ ")
    unwrapped = rendered.replace("\n", "").replace("│", "").replace(" ", "")
    assert "runtimev0.3.9" in unwrapped
    assert "http://runtime.test:7001" not in unwrapped
    assert "sandboxdocker·registry.example:5000/team/python:3.13-slim" in unwrapped
    assert "workspaceslab,toolang-with-a-long-workspace-name" in unwrapped
    assert rendered.count("·") == 1
    assert "Toolang Chat" not in rendered
    assert _CONTAINER_ID[:12] not in rendered


@pytest.mark.parametrize("width", [16, 18])
def test_chat_header_keeps_metadata_when_label_and_value_columns_cannot_fit(
    width: int,
) -> None:
    rendered = _render_text(
        blocks.HeaderBlock(
            client_version="0.4.0-client",
            executor_metadata=ChatExecutorMetadata(
                sandbox_driver="host",
                sandbox_detail=_HOST_DESCRIPTION,
                endpoint="http://runtime.test:7001",
                version="0.3.9",
                workspaces=("lab", "toolang"),
            ),
        ).render(),
        width=width,
    )
    assert all(get_cwidth(line) <= width for line in rendered.splitlines())
    unwrapped = rendered.replace("\n", "").replace("│", "").replace(" ", "")
    assert "runtimev0.3.9" in unwrapped
    assert "http://runtime.test:7001" not in unwrapped
    assert "sandbox" + _HOST_SANDBOX_VALUE.replace(" ", "") in unwrapped
    assert "workspaceslab,toolang" in unwrapped


@pytest.mark.parametrize(
    "version, expected",
    [
        ("0.3.8", "v0.3.8"),
        ("v0.3.9", "v0.3.9"),
        ("0.3.9*", "v0.3.9*"),
        ("0.4.0a2-25-g7297ecfd", "v0.4.0a2-25-g7297ecfd"),
        ("unknown", "unknown"),
    ],
)
@pytest.mark.parametrize("matching", [False, True])
def test_chat_header_keeps_client_and_runtime_versions(
    version: str, expected: str, matching: bool
) -> None:
    rendered = _render_text(
        blocks.HeaderBlock(
            client_version=version if matching else "0.4.0-client",
            executor_metadata=ChatExecutorMetadata(
                sandbox_driver="host",
                sandbox_detail=_HOST_DESCRIPTION,
                endpoint="http://runtime.test:7001",
                version=version,
                workspaces=("lab",),
            ),
        ).render(),
        width=120,
    )
    assert f"Chat {expected if matching else 'v0.4.0-client'}" in rendered
    runtime_line = next(line for line in rendered.splitlines() if "runtime" in line)
    assert runtime_line.split("runtime", 1)[1].strip("│ ") == expected
    assert "http://runtime.test:7001" not in rendered
    assert "Toolang Chat" not in rendered


@pytest.mark.parametrize(
    "workspaces, expected",
    [((), "none"), (None, "unavailable"), (("lab",), "lab")],
)
def test_chat_header_distinguishes_empty_and_unavailable_workspaces(
    workspaces: tuple[str, ...] | None, expected: str
) -> None:
    rendered = _render_text(
        blocks.HeaderBlock(
            client_version="0.4.0-client",
            executor_metadata=ChatExecutorMetadata(
                sandbox_driver="host",
                sandbox_detail=_HOST_DESCRIPTION,
                workspaces=workspaces,
            ),
        ).render(),
        width=100,
    )
    row = next(line for line in rendered.splitlines() if "workspaces" in line)
    assert row.split("workspaces", 1)[1].strip("│ ") == expected


@pytest.mark.parametrize(
    "driver, detail", [("host", _HOST_DESCRIPTION), ("docker", "python:3.13-slim")]
)
def test_chat_header_keeps_logo_styles_and_padding_without_links(
    driver: str, detail: str
) -> None:
    block = blocks.HeaderBlock(
        client_version="0.4.0-client",
        executor_metadata=ChatExecutorMetadata(
            sandbox_driver=driver,
            sandbox_detail=detail,
            endpoint="http://localhost:7001",
            version="v0.3.9",
            workspaces=("lab", "toolang"),
        ),
    )
    segments = rendering.render_segments(block.render(), width=100)
    rendered = _render_text(block.render(), width=100)
    assert f"{driver} · {detail}" in rendered
    assert _CONTAINER_ID[:12] not in rendered
    logo_blocks = [segment for segment in segments if "█" in segment.text]
    logo_dots = [segment for segment in segments if "⬤" in segment.text]
    keys = [
        next(segment for segment in segments if segment.text.strip() == key)
        for key in ("runtime", "sandbox", "workspaces")
    ]
    values = [
        next(segment for segment in segments if segment.text.strip() == value)
        for value in ("lab, toolang", "v0.3.9", driver, detail)
    ]
    separators = [segment for segment in segments if "·" in segment.text]
    assert "Toolang Chat" not in rendered
    assert "http://localhost:7001" not in rendered
    assert all(
        segment.style is None or segment.style.link is None for segment in segments
    )
    assert sum(segment.text.count("█") for segment in logo_blocks) == 15
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
    assert all(segment.style is not None and segment.style.dim for segment in keys)
    assert all(
        segment.style is None or (not segment.style.bold and not segment.style.dim)
        for segment in values
    )
    assert len(separators) == 1
    assert all(
        segment.style is not None and segment.style.dim for segment in separators
    )
    bordered = [line for line in rendered.splitlines() if line]
    assert not bordered[1].strip("│ ")
    assert not bordered[-2].strip("│ ")


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
                        "exhaustive": True,
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
                    reasoning=Reasoning(effort="high"),
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
        (Reasoning(effort="none"), "openai/gpt-5 · none"),
        (Reasoning(budget_tokens=4096), "openai/gpt-5 · 4096"),
    ],
)
def test_chat_model_label_preserves_explicit_reasoning_values(
    reasoning: Reasoning,
    expected: str,
) -> None:
    setting = SessionSetting(
        model=ModelRequest("openai/gpt-5", reasoning=reasoning),
        runnable="agic:chat",
    )

    assert slashes.chat_model_label({"items": []}, setting) == expected


@pytest.mark.parametrize("module", ["", "agent::"])
@pytest.mark.parametrize("runnable", ["agic:chat", "agic:main", "flow:main"])
def test_chat_status_bar_keeps_session_settings_at_the_edges(
    monkeypatch: Any,
    module: str,
    runnable: str,
) -> None:
    monkeypatch.setattr(widgets.StatusBar, "_terminal_width", staticmethod(lambda: 80))
    status = widgets.StatusBar(f"{module}{runnable}", "runtime model", "hak", "tq")
    idle = "".join(text for _style, text in status._render())

    status.set_run_workspace("tmp")
    status.set_running(True)
    running = "".join(text for _style, text in status._render())

    assert "^d exit" not in idle
    assert "↑↓ history" not in idle
    assert idle.startswith(f"{widgets._STATUS_INSET}{runnable}")
    assert "hak@tq" in idle
    assert running.startswith(f"{widgets._STATUS_INSET}{runnable}")
    assert "hak@tmp" in running
    assert "1m30s" not in running
    assert "running for" not in running
    assert "agent::" not in idle + running
    assert "tmp @ runtime model" not in running
    assert idle.endswith(f"runtime model{widgets._STATUS_INSET}")
    assert running.endswith(f"runtime model{widgets._STATUS_INSET}")
    assert idle.rindex("runtime model") == running.rindex("runtime model")
    assert get_cwidth(idle) == get_cwidth(running) == 80

    for line, center_label in ((idle, "hak@tq"), (running, "hak@tmp")):
        center = (
            get_cwidth(line[: line.index(center_label)]) + get_cwidth(center_label) / 2
        )
        assert center == pytest.approx(40, abs=0.5)


def test_chat_status_bar_keeps_center_agent_stable_across_run_lifecycle() -> None:
    status = widgets.StatusBar("agic:chat", "runtime model", "hak", "tq")
    idle = "".join(text for _style, text in status._render())

    status.set_run_workspace("tmp")
    status.set_running(True)
    running_fragments = status._render()
    running = "".join(text for _style, text in running_fragments)

    assert status._center_label() == "hak@tmp"
    assert "hak@tq" in idle
    assert "hak@tmp" in running
    assert "running" not in running
    for line, center_label in (
        (idle, "hak@tq"),
        (running, "hak@tmp"),
    ):
        center = (
            get_cwidth(line[: line.index(center_label)]) + get_cwidth(center_label) / 2
        )
        assert center == pytest.approx(40, abs=0.5)

    assert ("class:status.context", "hak") in running_fragments
    assert ("class:status.context.symbol", "@") in running_fragments
    assert ("class:status.context", "tmp") in running_fragments
    palette = widgets._chat_ui_palette()
    assert palette["status.context"] == ""
    assert palette["status.context.symbol"] == "dim"
    assert palette["status.elapsed"] == "dim"
    assert all(style != "class:status.elapsed" for style, _ in running_fragments)
    assert ("class:status.context.symbol", "@") in running_fragments

    status.set_running(False)
    stopped = "".join(text for _style, text in status._render())
    assert "hak@tq" in stopped
    assert "running" not in stopped
    assert "1s" not in stopped


def test_chat_status_bar_updates_only_the_session_setting_edges_during_a_run(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(widgets.StatusBar, "_terminal_width", staticmethod(lambda: 100))
    status = widgets.StatusBar("agic:chat", "model · auto", "hak", "session")
    status.set_run_workspace("run-space")
    status.set_running(True)

    status.set_status("flow:relay", "new-model · high", "new-session-space")
    text = "".join(fragment for _style, fragment in status._render())

    assert text.startswith(f"{widgets._STATUS_INSET}flow:relay")
    assert "hak@run-space" in text
    assert "running" not in text
    assert text.endswith(f"new-model · high{widgets._STATUS_INSET}")
    assert "new-session-space" not in text
    assert text.count("flow:relay") == 1
    assert "300k/1M" not in text
    assert "/" not in text


def test_chat_status_bar_keeps_center_and_truncates_edges_inward(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(widgets.StatusBar, "_terminal_width", staticmethod(lambda: 70))
    status = widgets.StatusBar(
        "flow:a_very_long_default_runnable",
        "openai/gpt-5 · high",
        "very-long-agent-name",
        "very-long-workspace-name",
    )
    status.set_running(True)

    text = "".join(fragment for _style, fragment in status._render())
    center_label = "very-long-agent-name@very-long-workspace-name"

    assert get_cwidth(text) == 70
    assert text.startswith(
        f"{widgets._STATUS_INSET}flow:a_v… {center_label} …-5 · high"
    )
    assert text.endswith(f"…-5 · high{widgets._STATUS_INSET}")
    assert f"{center_label} …-5 · high" in text
    assert text.count("…") == 2
    assert center_label in text
    center_start = text.index(center_label)
    center = get_cwidth(text[:center_start]) + get_cwidth(center_label) / 2
    assert center == pytest.approx(35, abs=0.5)


def test_chat_status_bar_reserves_no_visible_context_usage_content(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(widgets.StatusBar, "_terminal_width", staticmethod(lambda: 80))
    status = widgets.StatusBar("agic:chat", "runtime model", "hak", "toolang")
    idle = "".join(text for _style, text in status._render())
    status.set_running(True)
    running = "".join(text for _style, text in status._render())

    assert "hak@toolang" in idle
    assert idle == running
    assert "300k/1M" not in idle + running
    assert " / " not in idle + running
    assert idle.count("hak@toolang") == running.count("hak@toolang") == 1
    for line in (idle, running):
        label = "hak@toolang"
        center = get_cwidth(line[: line.index(label)]) + get_cwidth(label) / 2
        assert center == pytest.approx(40, abs=0.5)


@pytest.mark.parametrize("terminal_width", [1, 2, 5, 10, 20])
def test_chat_status_bar_never_overflows_exceptionally_narrow_terminals(
    monkeypatch: pytest.MonkeyPatch,
    terminal_width: int,
) -> None:
    monkeypatch.setattr(
        widgets.StatusBar,
        "_terminal_width",
        staticmethod(lambda: terminal_width),
    )
    status = widgets.StatusBar(
        "flow:a_very_long_default_runnable",
        "openai/a-very-long-model · high",
        "hak",
        "a-very-long-workspace",
    )
    status.set_running(True)

    text = "".join(fragment for _style, fragment in status._render())

    assert get_cwidth(text) == terminal_width
    if terminal_width <= 20:
        assert "flow:" not in text
        assert "openai/" not in text
        assert "1h" not in text


def test_chat_status_workspace_label_uses_workspace_uri_or_base() -> None:
    assert tui._workspace_label("tq://src/ui") == "tq"
    assert tui._workspace_label("src/ui", "lab://") == "lab"
    assert tui._workspace_label("/absolute/path") is None


def test_chat_status_resolves_absolute_and_default_session_workspaces() -> None:
    class WorkspaceClient(FakeClient):
        def resolve_workdir(
            self,
            workdir: str | None,
            workdir_base: str | None,
            thread_id: str | None,
        ) -> str:
            del workdir_base, thread_id
            return "repo://project" if workdir == "/private/project" else "lab://"

    client = WorkspaceClient()
    app = tui.ChatTuiApp(
        thread_id="term_workspace",
        setting=replace(
            client.initial_setting(),
            workdir="/private/project",
        ),
        input_history=None,
        client=client,
        agent_name="hak",
    )

    assert app.status_bar.workspace_label == "repo"
    app.status_bar.set_run_workspace(app._workspace_label_for("/private/project", None))
    app._set_status_running(True)
    assert app.status_bar._center_label() == "hak@repo"
    app.status_bar.set_running(False)

    app.setting = replace(app.setting, workdir=None, workdir_base=None)
    app.app_context.refresh_status()
    assert app.status_bar.workspace_label == "lab"


def test_chat_tui_tracks_only_root_chdir_workspace_in_center(
    monkeypatch: Any,
) -> None:
    app = tui.ChatTuiApp(
        thread_id="term_status",
        setting=SessionSetting(
            model=ModelRequest("openai/gpt-5"),
            runnable="agic:chat",
            workdir="session://",
        ),
        input_history=None,
        client=FakeClient(),
        agent_name="hak",
    )
    monkeypatch.setattr(tui.events, "handle_run_event", lambda _event, _app: None)
    app.status_bar.set_run_workspace("session")
    app._set_status_running(True)
    app.handle_run_event(_run_begin())

    app.handle_run_event(
        PartEnd(
            step=StepRef.parse("run_1.0"),
            part=0,
            data=ToolResultPart(
                tool_call_id="call_root",
                tool_name="_toolang__chdir",
                tool_family="_toolang",
                output={"cwd": "other://nested/path"},
            ),
        )
    )
    assert app.status_bar.run_workspace_label == "other"
    assert app.status_bar._center_label() == "hak@other"

    app.handle_run_event(
        PartEnd(
            step=StepRef.parse("run_child.0"),
            part=0,
            data=ToolResultPart(
                tool_call_id="call_child",
                tool_name="_toolang__chdir",
                tool_family="_toolang",
                output={"cwd": "child-workspace://"},
            ),
        )
    )
    assert app.status_bar.run_workspace_label == "other"
    assert "child-workspace" not in app.status_bar._center_label()


def test_chat_status_palette_has_no_marker_or_spinner_styles() -> None:
    palette = widgets._chat_ui_palette()

    assert palette["status"] == ""
    assert palette["status.context"] == ""
    assert palette["status.context.symbol"] == "dim"
    assert palette["status.elapsed"] == "dim"
    assert palette["status.error.marker"] == "fg:ansired"
    assert palette["status.error"] == "fg:ansired"
    assert (
        not {
            "status.marker",
            "status.spinner",
            "status.text",
            "status.agic",
            "status.flow",
            "status.model",
        }
        & palette.keys()
    )


def test_chat_status_refreshes_elapsed_time_without_animation_state() -> None:
    assert tui._STATUS_ELAPSED_TICK == pytest.approx(1.0)
    assert not hasattr(widgets, "_STATUS_SPINNER_STYLES")
    assert not hasattr(widgets.StatusBar("agic:chat", "model"), "spinner_index")
    assert not hasattr(tui, "_status_spinner_index")


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


def test_chat_tui_floors_status_elapsed_time() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        input_history=None,
        client=FakeClient(),
    )
    app._set_status_running(True)
    app._status_activity_started_at = 100.0

    app._update_status_elapsed(168.9)

    assert app.run_status_bar.elapsed_seconds == 68
    assert "1m8s" in "".join(text for _style, text in app.run_status_bar._render())

    app._update_status_elapsed(171.2)

    assert app.run_status_bar.elapsed_seconds == 71


def test_chat_ticker_refreshes_compact_progress_without_replacing_the_block() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        input_history=None,
        client=FakeClient(),
    )
    now = ["2026-01-01T00:00:00Z"]
    app.presenter._projector._clock = lambda: now[0]
    app._set_status_running(True)
    app._status_activity_started_at = 100.0
    app.presenter.handle(
        RunBegin(
            run="run_compact",
            control=ControlRef.for_run("run_compact", 0),
            runnable="agic:chat",
            started_at=now[0],
        ),
        app.app_context,
    )
    app.presenter.handle(
        StepBegin(
            step=StepRef.parse("run_compact.0"),
            kind="tool",
            started_at=now[0],
            given=ToolStepGiven(
                plugin="_toolang",
                trigger="runtime",
                call=ToolCall("compact", "compact", "_toolang__compact", {}),
                summary="Compacting thread history...",
            ),
        ),
        app.app_context,
    )
    (block,) = app.presenter._progress.values()
    now[0] = "2026-01-01T00:01:20Z"
    app._update_status_elapsed(180.0)
    assert next(iter(app.presenter._progress.values())) is block
    assert block.progress.rows[0].text.endswith("1m20s")
    app.presenter.reset()
    app._update_status_elapsed(181.0)
    assert not app.presenter._progress


def test_chat_tui_invalidates_only_when_the_visible_elapsed_second_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        input_history=None,
        client=FakeClient(),
    )
    app._set_status_running(True)
    app._status_activity_started_at = 100.0
    invalidations = 0

    def record_invalidation() -> None:
        nonlocal invalidations
        invalidations += 1

    monkeypatch.setattr(app, "_invalidate_ui", record_invalidation)

    app._update_status_elapsed(100.9)
    app._update_status_elapsed(101.1)
    app._update_status_elapsed(101.9)

    assert app.run_status_bar.elapsed_seconds == 1
    assert invalidations == 1


def test_chat_tui_repaints_run_elapsed_while_session_error_is_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        input_history=None,
        client=FakeClient(),
    )
    app._set_status_running(True)
    app.status_bar.set_error("Connection lost")
    app._status_activity_started_at = 100.0
    invalidations = 0

    def record_invalidation() -> None:
        nonlocal invalidations
        invalidations += 1

    monkeypatch.setattr(app, "_invalidate_ui", record_invalidation)

    app._update_status_elapsed(101.1)

    assert app.run_status_bar.elapsed_seconds == 1
    assert invalidations == 1
    assert "Connection lost" in "".join(text for _, text in app.status_bar._render())
    assert "1s" in "".join(text for _, text in app.run_status_bar._render())


def test_chat_tui_refreshes_elapsed_status_only_while_a_run_is_active(
    monkeypatch: Any,
) -> None:
    async def exercise() -> None:
        app = tui.ChatTuiApp(
            thread_id=None,
            setting=FakeClient().initial_setting(),
            input_history=None,
            client=FakeClient(),
        )
        app.loop = asyncio.get_running_loop()
        elapsed_updated = asyncio.Event()
        update_status_elapsed = app._update_status_elapsed

        def record_status_elapsed(now: float) -> None:
            update_status_elapsed(now)
            if app.run_status_bar.elapsed_seconds > 0:
                elapsed_updated.set()

        monkeypatch.setattr(app, "_update_status_elapsed", record_status_elapsed)
        refresh = asyncio.create_task(app._refresh_status_elapsed())
        try:
            app._set_status_running(True)
            assert app._status_activity_started_at is not None
            app._status_activity_started_at -= 1
            await asyncio.wait_for(elapsed_updated.wait(), timeout=0.5)

            app._set_status_running(False)

            assert app.run_status_bar.elapsed_seconds == 0
            assert not app.status_bar.running
        finally:
            refresh.cancel()
            with pytest.raises(asyncio.CancelledError):
                await refresh

    monkeypatch.setattr(tui, "_STATUS_ELAPSED_TICK", 0.001)
    asyncio.run(exercise())


def test_chat_tui_restarts_elapsed_refresh_timing_for_the_next_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        app = tui.ChatTuiApp(
            thread_id=None,
            setting=FakeClient().initial_setting(),
            input_history=None,
            client=FakeClient(),
        )
        app.loop = asyncio.get_running_loop()
        elapsed_updated = asyncio.Event()
        update_status_elapsed = app._update_status_elapsed

        def record_status_elapsed(now: float) -> None:
            update_status_elapsed(now)
            if app.run_status_bar.elapsed_seconds > 0:
                elapsed_updated.set()

        monkeypatch.setattr(app, "_update_status_elapsed", record_status_elapsed)
        refresh = asyncio.create_task(app._refresh_status_elapsed())
        try:
            app._set_status_running(True)
            await asyncio.sleep(0)

            app._set_status_running(False)
            app._set_status_running(True)
            assert app._status_activity_started_at is not None
            app._status_activity_started_at -= 1

            await asyncio.wait_for(elapsed_updated.wait(), timeout=0.1)
        finally:
            refresh.cancel()
            with pytest.raises(asyncio.CancelledError):
                await refresh

    monkeypatch.setattr(tui, "_STATUS_ELAPSED_TICK", 60.0)
    asyncio.run(exercise())


def test_chat_tui_stops_short_run_activity_immediately() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        input_history=None,
        client=FakeClient(),
    )
    app.status_bar.set_run_workspace("tmp")
    app._set_status_running(True)

    app._set_status_running(False)

    assert not app.status_bar.running
    assert app.status_bar.run_workspace_label == app.status_bar.workspace_label
    assert app.status_bar._center_label() == ""
    assert app.status_bar._render()[:2] == [
        ("class:status", widgets._STATUS_INSET),
        ("class:status", "agic:chat"),
    ]


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "canceled"])
def test_chat_tui_run_lifecycle_starts_and_stops_status_activity(
    outcome: Literal["succeeded", "failed", "canceled"],
) -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        input_history=None,
        client=FakeClient(),
    )

    app.submit_run(
        QueuedCall(
            "hello",
            RunRequest(
                thread_id="term_request",
                request_id="term_request",
                runnable=RunnableRequest("agic:chat", CallInput({"_": "hello"})),
                model=ModelRequest("openai/gpt-5"),
                policy=RunPolicy(),
            ),
        )
    )

    assert app.run_in_flight.is_set()
    assert app.status_bar.running
    assert app.run_status_bar.running
    app.run_status_bar.set_elapsed_seconds(80)

    app.active_run_id = "run_1"
    app.handle_run_event(_run_begin())
    if outcome == "canceled":
        app._request_run_cancel()
        assert app.run_status_bar.running
        assert app.run_status_bar.elapsed_seconds == 80
    app.handle_run_event(RunEnd(run="run_1", status=outcome))

    assert not app.run_in_flight.is_set()
    assert not app.status_bar.running
    assert not app.run_status_bar.running
    assert app.run_status_bar.elapsed_seconds == 0


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
        ("class:status", " " * 14),
        ("class:status", widgets._STATUS_INSET),
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

    assert text == "! First line se…" + widgets._STATUS_INSET
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
    assert text.startswith(f"{widgets._STATUS_INSET}flow:research")
    assert text.endswith(f"openai/o3{widgets._STATUS_INSET}")


def test_chat_tui_uses_truecolor_for_live_block_rendering() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
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
        input_history=None,
        client=UnavailableCatalogClient(),
    )

    assert app.status_bar.model_label == "openai/gpt-5"


def test_chat_tui_empty_enter_preserves_status_error() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
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


def test_chat_tui_keeps_session_runnable_when_run_events_arrive(
    monkeypatch: Any,
) -> None:
    app = tui.ChatTuiApp(
        thread_id="term_status",
        setting=SessionSetting(
            model=ModelRequest("openai/gpt-5"),
            runnable="flow:research",
            workdir="tq://",
        ),
        input_history=None,
        client=FakeClient(),
        agent_name="hak",
    )
    monkeypatch.setattr(tui.events, "handle_run_event", lambda _event, _app: None)
    app.status_bar.set_run_workspace("run-workspace")
    app._set_status_running(True)

    app.handle_run_event(_run_begin(runnable_name="review"))
    app.handle_run_event(
        _run_begin(
            run_id="run_child",
            parent_run_id="run_1",
            runnable_kind="flow",
            runnable_name="child",
        )
    )

    assert app._status_run_id == "run_1"
    assert app.status_bar.runnable_label == "flow:research"
    assert app.status_bar._center_label() == "hak@run-workspace"
    assert "review" not in app.status_bar._center_label()
    assert "child" not in app.status_bar._center_label()


def test_chat_tui_applies_default_settings_while_a_run_is_active() -> None:
    app = tui.ChatTuiApp(
        thread_id="term_status",
        setting=FakeClient().initial_setting(),
        input_history=None,
        client=FakeClient(),
        agent_name="hak",
    )
    app.active_run_id = "run_active"
    app._workdir_update_policies["run_active"] = (
        app._session_workdir_revision,
        True,
    )
    app.status_bar.set_run_workspace("active-space")
    app._set_status_running(True)

    app.handle_submit("/flow research")

    runnable_changed = "".join(text for _style, text in app.status_bar._render())
    assert app.status_bar.runnable_label == "flow:research"
    assert runnable_changed.startswith(f"{widgets._STATUS_INSET}flow:research")
    assert runnable_changed.count("flow:research") == 1
    assert "active-space" in runnable_changed
    assert "flow:research · openai/gpt-5" not in runnable_changed
    assert app.queue == []

    app.handle_submit("/model effort=high")

    model_changed = "".join(text for _style, text in app.status_bar._render())
    assert app.status_bar.model_label == "openai/gpt-5 · high"
    assert model_changed.endswith(f"openai/gpt-5 · high{widgets._STATUS_INSET}")
    assert model_changed.count("flow:research") == 1
    assert "active-space" in model_changed

    app.handle_submit("/cd next://src")

    cd_changed = "".join(text for _style, text in app.status_bar._render())
    assert app.status_bar.workspace_label == "next"
    assert app.status_bar.run_workspace_label == "active-space"
    assert "active-space" in cd_changed
    assert "next" not in cd_changed
    app._handle_run_state(RunWorkdirUpdated("run_active", "run-final://"))
    assert app.setting.workdir == "next://src"

    app.handle_submit("/agic chat")

    restored = "".join(text for _style, text in app.status_bar._render())
    assert restored.count("agic:chat") == 1
    assert restored.endswith(f"openai/gpt-5 · high{widgets._STATUS_INSET}")
    assert "active-space" in restored

    app._finish_active_run()
    stopped = "".join(text for _style, text in app.status_bar._render())
    assert "hak@next" in stopped
    assert "active-space" not in stopped


def test_chat_default_settings_clear_explicit_model_and_runnable() -> None:
    surface = SessionSetting(
        model=ModelRequest("openai/gpt-5"),
        runnable="agic:chat",
        runnable_follows_default=True,
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
        input_history=None,
        client=FailingClient(),
    )
    app.handle_submit("hello")

    assert app.status_bar.error_message == "thread creation failed"
    assert not app.status_bar.running
    assert not app.run_in_flight.is_set()


@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize(
    ("source", "runnable"),
    [
        (":flow research", "flow:research"),
        (":agic review", "agic:review"),
        (":runnable default", "agic:chat"),
    ],
)
def test_chat_tui_submits_empty_runnable_call(
    monkeypatch: Any, busy: bool, source: str, runnable: str
) -> None:
    class RunnableClient(FakeClient):
        def build_request(
            self,
            thread_id: str,
            override: RunOverride,
            input: CallInput[str],
            setting: SessionSetting,
        ) -> RunRequest:
            return build_run_request(
                thread_id=thread_id,
                request_id="term_request",
                input=input,
                override=override,
                setting=setting,
                surface=self.initial_setting(),
                resolve_model_ref=lambda ref: ref,
                resolve_runnable_ref=lambda ref: ref,
            )

    setting = replace(FakeClient().initial_setting(), runnable="agic:session")
    app = tui.ChatTuiApp(
        thread_id="term_test",
        setting=setting,
        input_history=None,
        client=RunnableClient(),
    )
    submitted: list[QueuedCall] = []
    monkeypatch.setattr(app, "submit_run", submitted.append)
    if busy:
        app.active_run_id = "run_busy"
    source = ":model effort=high\n" + source
    app.prompt.buffer.text = source

    app.handle_ui_event(ChatUIEvent("submit", source))

    calls = list(app.queue) if busy else submitted
    assert len(calls) == 1
    assert calls[0].request.runnable == RunnableRequest(runnable, CallInput())
    assert calls[0].request.model == ModelRequest(
        "openai/gpt-5", reasoning=Reasoning(effort="high")
    )
    assert app.setting == setting
    assert app.prompt.buffer.text == ""
    assert app.prompt.history.get_strings() == [source]
    assert app.status_bar.error_message == ""


def test_chat_queue_captures_settings_at_submission_time() -> None:
    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
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
        item.request.model.reasoning.effort
        for item in app.queue
        if item.request.model is not None and item.request.model.reasoning is not None
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
            on_control: Callable[[ControlInfo], None] | None = None,
        ) -> None:
            del on_error
            calls.append((run_id, message))
            steered.set()

    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
        input_history=None,
        client=RecordingClient(),
        progress_max_width=40,
    )
    app.active_run_id = "run_busy"
    app.prompt.buffer.text = "/help"

    app.handle_ui_event(ChatUIEvent("steer", "/help"))

    assert steered.wait(timeout=1)
    assert calls == [("run_busy", "/help")]
    assert app.prompt.buffer.text == ""
    assert app.prompt.history.get_strings() == ["/help"]
    steer_block = next(
        block
        for block in app.unfinalized_blocks
        if isinstance(block, blocks.RunSteerBlock)
    )
    assert steer_block.message == "/help"
    assert steer_block.max_width == 40


def test_chat_tui_meta_enter_without_an_active_run_preserves_the_draft() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        input_history=None,
        client=FakeClient(),
    )
    app.prompt.buffer.text = "keep this draft"

    app.handle_ui_event(ChatUIEvent("steer", "keep this draft"))

    assert app.prompt.buffer.text == "keep this draft"
    assert app.prompt.history.get_strings() == []
    assert app.status_bar.error_message == "No active run to steer"


def test_chat_tui_queue_panel_edit_refuses_to_overwrite_a_draft() -> None:
    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
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
            on_control: Callable[[ControlInfo], None] | None = None,
        ) -> None:
            del run_id, message, on_error
            steered.set()

    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
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
        input_history=None,
        client=FakeClient(),
    )
    rejected.queue.append(
        QueuedCall(
            "queued without run",
            FakeClient().build_request(
                "term_busy",
                RunOverride(),
                CallInput({"_": "queued without run"}),
                FakeClient().initial_setting(),
            ),
        )
    )
    rejected.queue_panel.reconcile()

    rejected._steer_selected_queue_item()

    assert [item.source for item in rejected.queue] == ["queued without run"]
    assert rejected.status_bar.error_message == "No active run to steer"


def test_chat_tui_queue_panel_delete_clamps_selection() -> None:
    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
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
        invoke(Keys.Tab)
        assert app.app.layout.current_control is app.queue_panel.view

        invoke(Keys.Down)
        assert app.queue_panel.selected_index == 1

        invoke(Keys.ControlP)
        assert app.queue_panel.selected_index == 0

        invoke(Keys.ControlN)
        assert app.queue_panel.selected_index == 1

        invoke(Keys.BackTab)
        assert isinstance(app.app.layout.current_control, BufferControl)
        assert app.app.layout.current_control.buffer is app.prompt.buffer

        invoke(Keys.BackTab)
        assert app.app.layout.current_control is app.queue_panel.view

        invoke(Keys.Tab)
        assert isinstance(app.app.layout.current_control, BufferControl)
        assert app.app.layout.current_control.buffer is app.prompt.buffer


def test_chat_tui_space_toggles_queue_and_tab_only_switches_focus() -> None:
    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
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
            assert app.ui_events.empty()
            assert app.prompt.buffer.text == ""
            assert any(
                isinstance(block, blocks.RunSteerBlock) and block.message == " ed"
                for block in app.unfinalized_blocks
            )
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
            while app.queue:
                app._pop_queued_call(0)
            assert app.queue_panel.expanded
            assert app.app.layout.current_control is not app.queue_panel.view

        await app.app.cancel_and_wait_for_background_tasks()

    asyncio.run(exercise())


@pytest.mark.parametrize("source", (":", "$", "@", "/"))
def test_chat_tui_namespace_input_does_not_interfere_with_queue_focus(
    source: str,
) -> None:
    class NoCompletionClient(FakeClient):
        def list_prompts(self, runnable: str | None = None) -> dict[str, object]:
            pytest.fail("Chat must not fetch a completion catalog")

    client = NoCompletionClient()
    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=client.initial_setting(),
        input_history=None,
        client=client,
    )
    app.active_run_id = "run_busy"
    app.handle_submit("queued input")
    app.prompt.buffer.document = Document(source, len(source))
    app.app_context.refresh_status()
    assert app.prompt.buffer.complete_state is None
    assert (
        list(
            app.prompt.buffer.completer.get_completions(
                app.prompt.buffer.document, CompleteEvent(completion_requested=True)
            )
        )
        == []
    )
    with set_app(app.app):
        bindings = app.app.key_bindings
        assert bindings is not None
        tab = [
            binding
            for binding in bindings.get_bindings_for_keys((Keys.Tab,))
            if binding.filter()
        ]
        assert len(tab) == 1
        tab[0].handler(cast(Any, SimpleNamespace()))
        assert app.app.layout.current_control is app.queue_panel.view
        assert app.prompt.buffer.text == source


def test_chat_tui_queue_steer_has_no_single_key_binding() -> None:
    assert shortcuts.QUEUE_STEER.bindings == shortcuts.STEER.bindings
    assert ("s",) not in shortcuts.QUEUE_STEER.bindings


@pytest.mark.parametrize("expanded", [False, True])
@pytest.mark.parametrize("queue_focused", [False, True])
@pytest.mark.parametrize(
    ("pressed", "event_type"),
    [
        ((Keys.Escape, Keys.Escape), "cancel"),
        ((Keys.ControlC,), "interrupt"),
    ],
)
def test_chat_run_and_input_controls_require_input_focus(
    monkeypatch: pytest.MonkeyPatch,
    expanded: bool,
    queue_focused: bool,
    pressed: tuple[Keys, ...],
    event_type: str,
) -> None:
    async def exercise() -> None:
        with create_app_session(input=DummyInput(), output=DummyOutput()):
            app = tui.ChatTuiApp(
                thread_id="term_busy",
                setting=FakeClient().initial_setting(),
                input_history=None,
                client=FakeClient(),
            )
            app.active_run_id = "run_busy"
            app.handle_submit("queued input")
            app.prompt.buffer.text = "keep draft"
            app.queue_panel.expanded = expanded
            app.app.timeoutlen = None
            cleared: list[bool] = []
            monkeypatch.setattr(app.app.renderer, "clear", lambda: cleared.append(True))
            with set_app(app.app):
                if queue_focused:
                    app.app.layout.focus(app.queue_panel.view)
                for key in pressed:
                    app.app.key_processor.feed(KeyPress(key))
                    app.app.key_processor.process_keys()
            if queue_focused:
                assert app.ui_events.empty()
            else:
                assert app.ui_events.get_nowait().type == event_type
                assert app.ui_events.empty()
            assert not cleared
            assert app.prompt.buffer.text == "keep draft"
            assert [item.source for item in app.queue] == ["queued input"]
            await app.app.cancel_and_wait_for_background_tasks()

    asyncio.run(exercise())


@pytest.mark.parametrize("queue_focused", [False, True])
def test_chat_ctrl_d_deletes_forward_while_the_draft_has_text(
    queue_focused: bool,
) -> None:
    """Ctrl+D keeps its terminal meaning and deletes forward, not the run."""

    async def exercise() -> None:
        async with _queue_test_app() as (app, _output):
            app.app.timeoutlen = None
            app.prompt.buffer.text = "keep draft"
            app.prompt.buffer.cursor_position = 5
            if queue_focused:
                app.app.layout.focus(app.queue_panel.view)
            app.app.key_processor.feed(KeyPress(Keys.ControlD))
            app.app.key_processor.process_keys()

            assert app.ui_events.empty()
            assert [item.source for item in app.queue] == ["first", "second", "third"]
            assert app.active_run_id == "run_busy"
            if queue_focused:
                assert app.prompt.buffer.text == "keep draft"
            else:
                assert app.prompt.buffer.text == "keep raft"
                assert app.prompt.buffer.cursor_position == 5

    asyncio.run(exercise())


@pytest.mark.parametrize("queue_focused", [False, True])
def test_chat_ctrl_d_exits_only_while_the_draft_is_empty(queue_focused: bool) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, _output):
            app.app.timeoutlen = None
            app.prompt.buffer.text = ""
            if queue_focused:
                app.app.layout.focus(app.queue_panel.view)
            app.app.key_processor.feed(KeyPress(Keys.ControlD))
            app.app.key_processor.process_keys()

            if queue_focused:
                assert app.ui_events.empty()
            else:
                assert app.ui_events.get_nowait().type == "eof"
                assert app.ui_events.empty()

    asyncio.run(exercise())


@pytest.mark.parametrize("expanded", [False, True])
@pytest.mark.parametrize("queue_focused", [False, True])
@pytest.mark.parametrize(
    ("key", "event_type"),
    [(Keys.Escape, None), (Keys.ControlL, "clear"), (Keys.ControlQ, "quit")],
)
def test_chat_shared_controls_keep_their_scope(
    expanded: bool, queue_focused: bool, key: Keys, event_type: str | None
) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, _output):
            app.queue_panel.expanded = expanded
            app.app.timeoutlen = None
            target = app.queue_panel.view if queue_focused else app.prompt.buffer
            app.app.layout.focus(target)
            before = app.app.layout.current_control
            app.status_bar.set_error("temporary status")
            app.app.key_processor.feed(KeyPress(key))
            app.app.key_processor.process_keys()

            assert app.app.layout.current_control is before
            if event_type is None:
                assert app.status_bar.error_message == ""
                assert app.ui_events.empty()
            else:
                assert app.ui_events.get_nowait().type == event_type
                assert app.ui_events.empty()

    asyncio.run(exercise())


@pytest.mark.parametrize("expanded", [False, True])
def test_chat_fifo_removal_preserves_selected_call_and_restores_empty_focus(
    monkeypatch: pytest.MonkeyPatch, expanded: bool
) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, _output):
            submitted: list[QueuedCall] = []
            monkeypatch.setattr(app, "submit_run", submitted.append)
            original = tuple(app.queue)
            app.queue_panel.move_selection(1)
            app.queue_panel.expanded = expanded
            app.app.layout.focus(app.queue_panel.view)

            app._finish_active_run()

            assert submitted == [original[0]]
            assert app.queue_panel.selected_index == 0
            assert app.queue[0] is original[1]
            assert app.queue_panel.expanded is expanded
            assert app.app.layout.current_control is app.queue_panel.view
            app._finish_active_run()
            app._finish_active_run()
            assert submitted == list(original)
            assert app.queue_panel.selected_index is None
            assert app.queue_panel.expanded
            assert app.app.layout.current_control is not app.queue_panel.view

    asyncio.run(exercise())


@pytest.mark.parametrize("command", ["queue", "q", "steer", "s"])
def test_chat_removed_queue_commands_preserve_queue_selection_and_draft(
    command: str,
) -> None:
    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
        input_history=None,
        client=FakeClient(),
    )
    app.active_run_id = "run_busy"
    for source in ("first", "second", "third"):
        app.handle_submit(source)
    app.queue_panel.move_selection(1)
    app.prompt.buffer.text = f"/{command} delete 1"

    assert not app.handle_submit(app.prompt.buffer.text)

    assert [item.source for item in app.queue] == ["first", "second", "third"]
    assert app.queue_panel.selected_index == 1
    assert app.prompt.buffer.text == f"/{command} delete 1"
    assert app.status_bar.error_message == slashes.unrecognized_diagnostic(command)


def test_chat_queue_shortcut_help_includes_navigation_and_contextual_actions() -> None:
    lines = shortcuts.help_lines()
    queue_lines = lines[lines.index("Queue focused:") + 1 : lines.index("Global:") - 1]

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
    assert shortcuts.QUEUE_TOGGLE.hint("Expand") == "space expand"
    assert shortcuts.SWITCH_AREA.hint_phrase("Focus") == "tab to focus"
    assert shortcuts.QUEUE_TOGGLE.hint_phrase("Expand") == "space to expand"
    assert shortcuts.QUEUE_TOGGLE.hint_phrase("Collapse") == "space to collapse"
    assert shortcuts.QUEUE_EDIT.hint("Edit") == "e edit"
    assert shortcuts.QUEUE_STEER.hint("Steer") == "m-enter steer"
    assert shortcuts.QUEUE_DELETE.hint("Delete") == "d delete"
    assert shortcuts.QUEUE_STEER.help_label == "Meta+Enter"
    assert shortcuts.QUEUE_DELETE.help_label == "d (Del)"
    assert shortcuts.SWITCH_AREA.help_label == "Tab (Shift+Tab)"
    assert shortcuts.CANCEL_RUN.help_label == "Esc Esc"
    assert shortcuts.INSERT_NEWLINE.label == "Ctrl+J"
    assert "also Shift+Enter if supported" in shortcuts.INSERT_NEWLINE.summary
    assert shortcuts.QUEUE_EDIT.bindings == (("e",),)
    assert shortcuts.QUEUE_DELETE.bindings == (("d",), ("delete",))
    assert shortcuts.QUEUE_STEER.bindings == (("escape", "enter"),)

    lines = shortcuts.help_lines()
    groups = (
        ("Input focused:", shortcuts.INPUT_SHORTCUTS),
        ("Queue focused:", shortcuts.QUEUE_SHORTCUTS),
        ("Global:", shortcuts.GLOBAL_SHORTCUTS),
    )
    summary_columns: set[int] = set()
    for title, group in groups:
        start = lines.index(title) + 1
        for shortcut, line in zip(
            group, lines[start : start + len(group)], strict=True
        ):
            assert line.startswith(shortcut.help_label + "  ")
            assert line.endswith(shortcut.summary)
            summary_columns.add(line.index(shortcut.summary))
    assert len(summary_columns) == 1
    assert shortcuts.CANCEL_RUN in shortcuts.INPUT_SHORTCUTS
    assert shortcuts.INTERRUPT in shortcuts.INPUT_SHORTCUTS
    assert shortcuts.EOF in shortcuts.INPUT_SHORTCUTS
    assert shortcuts.CLEAR in shortcuts.GLOBAL_SHORTCUTS
    assert shortcuts.QUIT in shortcuts.GLOBAL_SHORTCUTS


def test_chat_tui_queue_blocks_mutations_after_ambiguous_acceptance() -> None:
    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=FakeClient().initial_setting(),
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
        lambda values, **_kwargs: rendered.extend(
            _render_text(value) for value in values
        ),
    )
    app = tui.ChatTuiApp(
        thread_id="term_remote",
        setting=FakeClient().initial_setting(),
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
                CallInput({"_": "already queued"}),
                FakeClient().initial_setting(),
            ),
        )
    )
    message = "Restart Chat before submitting again."

    app.handle_ui_event(ChatUIEvent("run_state", RunBlocked(None, message)))
    app.handle_submit("new call")
    app.handle_submit("/model test/model")
    app.handle_submit("/keys")
    app.handle_submit("/output run_remote")

    assert app.submission_blocked == message
    assert [item.source for item in app.queue] == ["already queued"]
    assert app.setting == FakeClient().initial_setting()
    assert app.run_in_flight.is_set()
    assert app.status_bar.error_message == f"Submissions paused: {message}"
    assert any("Queue focused:" in value for value in rendered)
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
    assert any("/model [MODEL] [PARAM=VALUE...]" in value for value in rendered)
    assert any(
        "Set the session model or its parameters." in value for value in rendered
    )
    assert client.model_reads == initial_reads


def test_chat_tui_routes_slash_shaped_parse_errors_to_scrollback(
    monkeypatch: Any,
) -> None:
    app = tui.ChatTuiApp(
        thread_id="term_remote",
        setting=FakeClient().initial_setting(),
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
            "Include primary or named input, or select a runnable · See :? for help",
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
        ("/model", "/model [MODEL] [PARAM=VALUE...]"),
        ("/help\nInput", "Error: quick command cannot be combined with other input"),
        ("/?", "Session"),
        (":?", "Overrides apply to this run only; session defaults stay unchanged."),
        ("/keys", "Input focused:"),
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
            input: CallInput[str],
            setting: SessionSetting,
        ) -> RunRequest:
            del thread_id, override, input, setting
            raise ValueError("selected model is unavailable")

    app = tui.ChatTuiApp(
        thread_id="term_remote",
        setting=FakeClient().initial_setting(),
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
            input: CallInput[str],
            setting: SessionSetting,
        ) -> RunRequest:
            del override, setting
            return RunRequest(
                thread_id=thread_id,
                request_id="term_request",
                runnable=RunnableRequest("agic:chat", input),
                model=ModelRequest(
                    "openai/gpt-5",
                    reasoning=Reasoning(effort="medium"),
                ),
                policy=RunPolicy(),
            )

    source = ":model effort=medium\nhello"
    app = tui.ChatTuiApp(
        thread_id="term_remote",
        setting=FakeClient().initial_setting(),
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


def test_chat_tui_uses_queued_workspace_snapshot_for_the_next_active_status() -> None:
    app = tui.ChatTuiApp(
        thread_id="term_busy",
        setting=SessionSetting(
            model=ModelRequest("openai/gpt-5"), runnable="agic:chat"
        ),
        input_history=None,
        client=FakeClient(),
    )
    app.active_run_id = "run_busy"
    app.run_in_flight.set()
    app.status_bar.set_run_workspace("current")
    app._set_status_running(True)
    app.queue.append(
        QueuedCall(
            "queued",
            RunRequest(
                thread_id="term_busy",
                request_id="term_queued",
                runnable=RunnableRequest("flow:research", CallInput({"_": "queued"})),
                model=ModelRequest("openai/gpt-5"),
                policy=RunPolicy(),
                workdir="lab://queued",
            ),
        )
    )

    app.run_status_bar.set_elapsed_seconds(80)
    app._finish_active_run()

    assert app.run_status_bar.running
    assert app.run_status_bar.elapsed_seconds == 0
    assert app.run_status_bar._elapsed_label() == "Working"
    assert app.status_bar.running
    assert app.status_bar.runnable_label == "agic:chat"
    assert app.status_bar.run_workspace_label == "lab"
    assert app.status_bar._center_label() == "lab"
    assert app.run_in_flight.is_set()


def test_chat_tui_empty_input_requires_two_interrupts_to_exit() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
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
        input_history=None,
        client=FakeClient(),
    )

    assert app.handle_ui_event(ChatUIEvent("interrupt")) is False
    app.prompt.buffer.text = "hello"

    assert not app.interrupt_exit_pending
    assert app.status_bar.error_message == ""


@pytest.mark.parametrize("rows", [4, 5, 6, 30])
def test_chat_initial_input_and_first_control_share_the_live_origin(
    rows: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        output = _TerminalOutput()
        output.rows = rows
        with create_app_session(input=DummyInput(), output=output):
            app = tui.ChatTuiApp(
                thread_id="term_new",
                setting=FakeClient().initial_setting(),
                input_history=None,
                client=FakeClient(),
            )
            monkeypatch.setattr(tui.threading.Thread, "start", lambda self: None)
            with set_app(app.app):
                app.prompt.replace_input("hello")
                initial = _render_chat_layout(app)
                assert "class:input" in initial.data_buffer[0][1].style
                assert _screen_lines(initial, output.columns)[1].strip() == "hello"

                # Give the submitted control room to remain fully visible.
                output.rows = 30
                app.handle_ui_event(ChatUIEvent("submit", "hello"))
                submitted = _render_chat_layout(app)
                lines = _screen_lines(submitted, output.columns)
                assert "hello" in lines[1]
                assert any("Working" in line for line in lines)
                assert "Describe your task" in "\n".join(lines)
            await app.app.cancel_and_wait_for_background_tasks()

    asyncio.run(exercise())


@pytest.mark.parametrize("rows", [4, 5, 6, 30])
def test_chat_tui_clear_collapses_run_spacing_until_next_run(rows: int) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, output):
            output.rows = rows
            app.queue.clear()
            app._finish_active_run()
            app.prompt.replace_input("draft")
            _render_chat_layout(app)

            for _ in range(2):
                app._handle_clear()
                screen = _render_chat_layout(app)
                lines = _screen_lines(screen, output.columns)
                assert lines[1].strip() == "draft"
                assert "class:input" in screen.data_buffer[0][1].style
                assert app._run_status_rows() == 0

            app._set_status_running(True)
            assert app._run_status_rows() == min(2, rows - 4)
            running = _render_chat_layout(app)
            running_lines = _screen_lines(running, output.columns)
            input_row = next(
                i for i, line in enumerate(running_lines) if "draft" in line
            )
            if rows > 4:
                assert running_lines[input_row - 2].strip() == "Working"

            app._finish_active_run()
            stopped_lines = _screen_lines(_render_chat_layout(app), output.columns)
            assert "draft" in stopped_lines[input_row]
            assert not any("Working" in line for line in stopped_lines)
            assert app._run_status_rows() == min(2, rows - 4)

    asyncio.run(exercise())


@pytest.mark.parametrize("active_run, in_flight", [(True, False), (False, True)])
def test_chat_tui_rejected_clear_preserves_run_spacing(
    active_run: bool, in_flight: bool
) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, output):
            app.queue.clear()
            app.active_run_id = "run_busy" if active_run else None
            if in_flight:
                app.run_in_flight.set()
            app._set_status_running(True)
            before = _screen_lines(_render_chat_layout(app), output.columns)
            app._handle_clear()
            after = _screen_lines(_render_chat_layout(app), output.columns)
            assert app._run_status_rows() == 2
            assert next(
                i for i, line in enumerate(before) if "Working" in line
            ) == next(i for i, line in enumerate(after) if "Working" in line)
            assert "Wait for the active run" in app.status_bar.error_message

    asyncio.run(exercise())


def test_chat_tui_clear_scrolls_one_separator_into_history_before_redrawing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
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
        input_history=None,
        client=FakeClient(),
    )
    block = blocks.RunSummaryBlock.create(_run_begin())
    block.update(_run_end(status="canceled"))
    app.unfinalized_blocks.append(block)

    def write_renderables(
        renderables: Sequence[RenderableType | None], **_kwargs: Any
    ) -> None:
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
        assert "/model [MODEL] [PARAM=VALUE...]" in _render_text(renderables[0])
        order.append("write")

    monkeypatch.setattr(app, "_write_scrollback", write_scrollback)
    monkeypatch.setattr(app.app, "invalidate", lambda: order.append("invalidate"))

    app.handle_submit("/model")

    assert order == []
    assert len(app._pending_scrollback) == 1

    app._commit_ui_update()

    assert order == ["erase:False", "write", "invalidate"]
    assert app._pending_scrollback == []


def test_chat_tui_adds_one_trailing_gap_to_summary_only_output(
    monkeypatch: Any,
) -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        setting=FakeClient().initial_setting(),
        input_history=None,
        client=FakeClient(),
    )
    written: list[RenderableType | None] = []
    monkeypatch.setattr(
        tui.rendering,
        "write_renderables",
        lambda renderables, **_kwargs: written.extend(renderables),
    )

    app.handle_submit("/output first second")

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
    assert "Thinking" in "".join(
        _render_text(block.render()) for block in app.unfinalized_blocks
    )
    app._commit_ui_update()

    app.handle_run_event(
        StepEnd(
            step=StepRef.parse("run_1.1"),
            kind="model",
            status="failed",
            error=ErrorMessage("You have no credits remaining."),
            finished_at="2026-01-01T00:00:02Z",
        )
    )
    app._commit_ui_update()

    assert erases == [False]
    assert len(writes) == 1
    assert "Thinking" not in writes[0]
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
        input_history=None,
        client=FakeClient(),
    )
    written: list[RenderableType | None] = []
    monkeypatch.setattr(
        tui.rendering,
        "write_renderables",
        lambda renderables, **_kwargs: written.extend(renderables),
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
        control=ControlRef.for_run(run_id, 0),
        parent=(
            StepRef.parse(f"{parent_run_id}.2") if parent_run_id is not None else None
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
            _output(StepRef.parse(f"{run_id}.{output_step_index}"))
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
        step=StepRef.parse(f"{run_id}.{step_index}"),
        kind="model",
        input=(),
        given=_model_given(model or "test/model"),
        started_at="2026-01-01T00:00:01Z",
    )


def _tool_step_begin(*, step_index: int = 1) -> StepBegin:
    return StepBegin(
        step=StepRef.parse(f"run_1.{step_index}"),
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
        step=StepRef.parse(f"{run_id}.{step_index}"),
        kind="model",
        status="succeeded",
        output=_parts(TextPart(text=output)),
        noted=ModelStepNoted(
            accounting=ModelAccounting(input_tokens=1, output_tokens=1)
        ),
        finished_at=finished_at,
    )


def _flow_step_begin(*, step_index: int = 1) -> StepBegin:
    return StepBegin(
        step=StepRef.parse(f"run_1.{step_index}"),
        kind="par",
        input=(),
        started_at="2026-01-01T00:00:01Z",
        given=MapStmt(span=Span(line=1), runnable="summarize", lanes=2),
    )


def _flow_step_end(*, step_index: int = 1) -> StepEnd:
    return StepEnd(
        step=StepRef.parse(f"run_1.{step_index}"),
        kind="par",
        status="succeeded",
        output=Output(value_for_type("Json[]", ()), "_"),
        finished_at="2026-01-01T00:00:02Z",
    )


def _child_run_step_begin(
    *, step_index: int = 2, step: StepRef | str | None = None
) -> StepBegin:
    return StepBegin(
        step=StepRef.parse(step or f"run_1.{step_index}"),
        kind="run",
        input=(),
        started_at="2026-01-01T00:00:01Z",
        given=RunStmt(span=Span(line=1), runnable="summarize"),
    )


def _child_run_step_end(
    *, step_index: int = 2, step: StepRef | str | None = None
) -> StepEnd:
    return StepEnd(
        step=StepRef.parse(step or f"run_1.{step_index}"),
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
        step=StepRef.parse(f"{run_id}.{step_index}"),
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
        self.live_blocks[:] = [item for item in self.live_blocks if item is not block]
        self.finalized.append(block)

    def finish_run(self) -> None:
        self.active_run = None
        self.finished = True

    def refresh_status(self) -> None:
        pass

    def request_exit(self) -> None:
        pass


class FakeClient(ChatClient):
    executor_metadata = ChatExecutorMetadata(
        sandbox_driver="host",
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
                    "parameters": {
                        "reasoning": {"effort": ["low", "high"], "exhaustive": True}
                    },
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

    def resolve_workdir(
        self,
        workdir: str | None,
        workdir_base: str | None,
        thread_id: str | None,
    ) -> str:
        del thread_id
        for value in (workdir, workdir_base):
            if isinstance(value, str) and "://" in value:
                return value
        raise ToolangError("fake client cannot resolve this workdir")

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
        input: CallInput[str],
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
        on_control: Callable[[ControlInfo], None] | None = None,
    ) -> None:
        del run_id, message, on_error


def _steer_control(index: int, *, run_id: str = "run_1") -> ControlInfo:
    value = _parts(*Message.user("adjust").parts).value
    assert isinstance(value, Array)
    return ControlInfo(
        run_id=run_id,
        index=index,
        kind="steer",
        timing="next_step",
        request_id=f"request_{index}",
        status="pending",
        payload=SteerControlPayload(CallInput({"_": value})),
        error=None,
        created_at="2026-01-01T00:00:01Z",
        finished_at=None,
    )


def _submit_test_steer(
    app: FakeApp, key: str, message: str = "adjust"
) -> blocks.RunSteerBlock:
    block = blocks.RunSteerBlock.create(message=message, run_id="run_1", max_width=60)
    app.presenter.add_steer(key, block, app)
    return block


def _steer_feedback(app: FakeApp) -> str:
    return "".join(
        _render_text(block.render())
        for block in app.live_blocks
        if isinstance(block, blocks.SteerFeedbackBlock)
    )


def test_chat_steers_match_receipts_and_consumption_without_changing_accents() -> None:
    app = FakeApp()
    events.handle_run_event(_run_begin(), app)
    events.handle_run_event(_model_step_begin(), app)
    steers = [_submit_test_steer(app, str(i), "identical") for i in range(3)]
    before = [_render_text(s.render()) for s in steers]
    assert _steer_feedback(app).strip() == "• 3 steers pending"
    for i in (2, 0):
        app.presenter.handle_steer_receipt(
            SteerReceipt(str(i), "run_1", _steer_control(i + 1)), app
        )
    assert _steer_feedback(app).strip() == "• 3 steers pending"
    assert [b for b in app.live_blocks if isinstance(b, blocks.RunSteerBlock)] == steers
    app.presenter.handle_steer_receipt(
        SteerReceipt("1", "run_1", _steer_control(2)), app
    )
    events.handle_run_event(_model_step_begin(run_id="run_child"), app)
    events.handle_run_event(_tool_step_begin(step_index=2), app)
    assert all(s in app.live_blocks for s in steers)
    assert _steer_feedback(app).strip() == "• 3 steers pending"
    events.handle_run_event(
        replace(
            _model_step_begin(step_index=3),
            preceded_by=(
                ControlRef.for_run("run_1", 1),
                ControlRef.for_run("run_1", 3),
            ),
        ),
        app,
    )
    assert any(b is steers[0] for b in app.finalized)
    assert any(b is steers[2] for b in app.finalized)
    assert any(b is steers[1] for b in app.live_blocks)
    assert _steer_feedback(app).strip() == "• 1 steer pending"
    events.handle_run_event(
        replace(
            _model_step_begin(step_index=4),
            preceded_by=(ControlRef.for_run("run_1", 2),),
        ),
        app,
    )
    assert not _steer_feedback(app)
    assert [_render_text(s.render()) for s in steers] == before
    assert [s for s in app.finalized if isinstance(s, blocks.RunSteerBlock)] == [
        steers[0],
        steers[2],
        steers[1],
    ]


def test_chat_steer_consumption_before_receipt_and_replay_are_idempotent() -> None:
    app = FakeApp()
    events.handle_run_event(_run_begin(), app)
    steer = _submit_test_steer(app, "early")
    consumed = replace(
        _model_step_begin(), preceded_by=(ControlRef.for_run("run_1", 8),)
    )
    events.handle_run_event(consumed, app)
    assert steer in app.live_blocks
    receipt = SteerReceipt("early", "run_1", _steer_control(8))
    app.presenter.handle_steer_receipt(receipt, app)
    events.handle_run_event(consumed, app)
    app.presenter.handle_steer_receipt(receipt, app)
    assert sum(b is steer for b in app.finalized) == 1
    assert not _steer_feedback(app)


@pytest.mark.parametrize("status", ["succeeded", "failed", "canceled"])
@pytest.mark.parametrize("disconnected", [False, True])
def test_chat_terminal_steer_labels_require_known_non_adoption(
    status: Any, disconnected: bool
) -> None:
    app = FakeApp()
    events.handle_run_event(_run_begin(), app)
    accepted = _submit_test_steer(app, "accepted")
    sending = _submit_test_steer(app, "sending")
    app.presenter.handle_steer_receipt(
        SteerReceipt("accepted", "run_1", _steer_control(1)), app
    )
    assert _steer_feedback(app).strip() == "• 2 steers pending"
    if disconnected:
        app.presenter.mark_disconnected()
    events.handle_run_event(_run_end(status=status), app)
    assert accepted.not_applied is not disconnected
    assert not sending.not_applied
    assert not _steer_feedback(app)
    assert "Steer not applied" not in "".join(
        _render_text(b.render()) for b in app.finalized
    )
    events.handle_run_event(_run_begin(run_id="run_next"), app)
    app.presenter.handle_steer_receipt(
        SteerReceipt("sending", "run_1", _steer_control(2)), app
    )
    assert not app.presenter.handle_steer_error(
        SteerError("sending", "run_1", "late"), app
    )
    assert not _steer_feedback(app)


def test_chat_failed_steer_keeps_message_with_error_and_updates_count() -> None:
    app = FakeApp()
    events.handle_run_event(_run_begin(), app)
    failed = _submit_test_steer(app, "failed", "original message")
    _submit_test_steer(app, "pending", "different message")
    error = SteerError("failed", "run_1", "connection lost")
    assert app.presenter.handle_steer_error(error, app)
    assert not app.presenter.handle_steer_error(error, app)
    assert app.finalized[-2] is failed
    assert isinstance(app.finalized[-1], blocks.SubmissionErrorBlock)
    assert "connection lost" in _render_text(app.finalized[-1].render())
    assert not failed.not_applied
    assert _steer_feedback(app).strip() == "• 1 steer pending"


@pytest.mark.parametrize(
    "control_status", ["applied", "wontapply", "revoked", "pending"]
)
def test_chat_recovered_controls_determine_terminal_corner(control_status: Any) -> None:
    app = FakeApp()
    events.handle_run_event(_run_begin(), app)
    steer = _submit_test_steer(app, "recover")
    receipt = _steer_control(1)
    app.presenter.handle_steer_receipt(SteerReceipt("recover", "run_1", receipt), app)
    detail = RunDetail(
        id="run_1",
        parent=None,
        thread_id="term_1",
        root_run_id="run_1",
        runnable_kind="agic",
        runnable_name="chat",
        call_kind="top",
        state=RunControlRefData(run="run_1", index=0),
        occurrence=None,
        input_text="hello",
        summary="done",
        status="canceled",
        error=None,
        created_at="2026-01-01T00:00:00Z",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:03Z",
        updated_at="2026-01-01T00:00:03Z",
        control=RunControlRefData(run="run_1", index=0),
        output=None,
        controls=[replace(receipt, status=control_status)],
        steps=[],
    )
    assert app.presenter.handle_recovered(app, detail)
    assert steer.not_applied is (control_status != "applied")
    assert not _steer_feedback(app)
    assert sum(b is steer for b in app.finalized) == 1


@pytest.mark.parametrize(
    ("model", "model_label"),
    [
        (None, "model unspecified"),
        (ModelRequest("openai/gpt-5"), "openai/gpt-5 · auto"),
        (
            ModelRequest(
                "openai/gpt-5",
                reasoning=Reasoning(effort="high"),
            ),
            "openai/gpt-5 · high",
        ),
        (
            ModelRequest(
                "test/model",
                reasoning=Reasoning(budget_tokens=4096),
            ),
            "test/model · 4096",
        ),
    ],
)
@pytest.mark.parametrize("module", ["", "agent::"])
def test_chat_root_context_uses_request_and_authoritative_runnable(
    model: ModelRequest | None,
    model_label: str,
    module: str,
) -> None:
    request = RunRequest(
        thread_id="term_1",
        request_id="one",
        runnable=RunnableRequest(
            f"{module}agic:provisional", CallInput({"_": "hello"})
        ),
        model=model,
        policy=RunPolicy(),
    )
    block = blocks.RunControlBlock.create("hello", request=request)
    assert block.runnable == f"{module}agic:provisional"
    assert (
        _render_text(block.render(), width=80).splitlines()[-1].strip()
        == "agic:provisional · " + model_label
    )
    block.update(replace(_run_begin(), runnable=f"{module}agic:research"))
    assert block.runnable == f"{module}agic:research"
    expected = "agic:research · " + model_label
    lines = _render_text(block.render(), width=80).splitlines()
    assert len(lines) == 3
    assert lines[-1].strip() == expected
    assert lines[-1].endswith(expected + "  ")
    assert lines[1].removeprefix(rendering.CONTROL_BAR_MARK).strip() == "hello"
    segments = rendering.render_segments(block.render(), width=80)
    annotation = next(s for s in segments if expected in s.text)
    assert annotation.style is not None and annotation.style.dim


@pytest.mark.parametrize("width", [8, 20, 40, 80])
def test_chat_context_and_steer_corners_fit_without_losing_padding(width: int) -> None:
    request = RunRequest(
        thread_id="term_1",
        request_id="one",
        runnable=RunnableRequest(
            "agic:研究研究研究研究研究", CallInput({"_": "hello"})
        ),
        model=ModelRequest(
            "provider/a-very-long-model",
            reasoning=Reasoning(effort="high"),
        ),
        policy=RunPolicy(),
    )
    block = blocks.RunControlBlock.create("ab\ncd\nef", request=request)
    lines = _render_text(block.render(), width=width).splitlines()
    assert len(lines) == 5
    assert all(get_cwidth(line) == width for line in lines)
    assert not lines[0].strip()
    assert all(line.endswith("  ") for line in lines)
    if width >= 20:
        assert lines[-1].endswith(" · high  ")
    steer = blocks.RunSteerBlock.create(message="hello", run_id="run_1", max_width=40)
    before = rendering.render_segments(steer.render(), width=width)
    steer.not_applied = True
    after = rendering.render_segments(steer.render(), width=width)

    def accents(segments: list[Segment]) -> list[object]:
        return [
            s.style
            for s in segments
            if s.text == rendering.CONTROL_BAR_MARK
            and s.style
            and s.style.color
            and s.style.color.get_truecolor().hex
            == Color.parse(rendering.STEER_CONTROL_ACCENT).get_truecolor().hex
        ]

    assert accents(before) == accents(after)
    assert _render_text(steer.render(), width=width).splitlines()[-1].endswith("  ")
    if width >= 20:
        assert (
            _render_text(steer.render(), width=width)
            .splitlines()[-1]
            .endswith("not applied  ")
        )


@pytest.mark.parametrize("active_step", [False, True])
@pytest.mark.parametrize(
    ("accepted", "sending", "message"),
    [
        (0, 1, "• 1 steer pending"),
        (1, 0, "• 1 steer pending"),
        (0, 3, "• 3 steers pending"),
        (3, 0, "• 3 steers pending"),
        (3, 1, "• 4 steers pending"),
    ],
)
def test_chat_steer_feedback_is_dim_with_one_combined_pending_count(
    active_step: bool, accepted: int, sending: int, message: str
) -> None:
    feedback = blocks.SteerFeedbackBlock(
        accepted=accepted, sending=sending, active_step=active_step
    )
    segments = rendering.render_segments(feedback.render(), width=80)
    assert "".join(segment.text for segment in segments).strip() == message
    assert all(
        segment.style is not None and segment.style.dim
        for segment in segments
        if segment.text.strip()
    )
    fragments = rendering.renderable_to_prompt_toolkit(feedback.render())
    assert "".join(fragment[1] for fragment in fragments).strip() == message
    assert all(
        "dim" in fragment[0].split() for fragment in fragments if fragment[1].strip()
    )


def test_chat_steer_feedback_has_blank_rows_and_wraps_after_the_marker() -> None:
    feedback = blocks.SteerFeedbackBlock(accepted=3, active_step=True, max_width=14)
    lines = _render_text(feedback.render(), width=14).splitlines()
    assert lines[0] == lines[-1] == ""
    assert rendering.renderables_height([feedback]) == len(lines)
    lines = lines[1:-1]
    assert lines[0].startswith("• ")
    assert all(line.startswith("  ") for line in lines[1:])
    assert len(lines) > 1
    assert all(get_cwidth(line) <= 12 for line in lines)
    assert " ".join(line[2:] for line in lines) == "3 steers pending"


@pytest.mark.parametrize("accepted", [1, 3])
def test_chat_live_steer_feedback_has_blank_rows_before_queue(accepted: int) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, output):
            app.unfinalized_blocks.extend(
                [
                    blocks.RunSteerBlock.create(message="adjust", run_id="run_busy"),
                    blocks.SteerFeedbackBlock(accepted=accepted, active_step=True),
                ]
            )
            screen = _render_chat_layout(app)
            lines = _screen_lines(screen, output.columns)
            status_row = next(i for i, line in enumerate(lines) if "• " in line)
            assert "pending" in lines[status_row]
            assert not lines[status_row - 1].strip()
            assert not lines[status_row + 1].strip()
            assert not lines[status_row + 2].strip()
            assert lines[status_row + 3].strip() == "Working"
            assert "3 queued" in lines[status_row + 4]
            # The upper gap is outside the padded control bar.
            assert _cell_attrs(app, screen, status_row - 1, 0).bgcolor == ""

    asyncio.run(exercise())


@pytest.mark.parametrize("focused", [False, True])
def test_chat_short_live_view_keeps_steer_feedback_and_queue_focus(
    focused: bool,
) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, output):
            output.rows = 12
            output.columns = 80
            if focused:
                app.app.layout.focus(app.queue_panel.view)
            original_focus = app.app.layout.current_control
            presenter = app.app_context.get_presenter()
            for i in range(3):
                presenter.add_steer(
                    str(i),
                    blocks.RunSteerBlock.create(
                        message="long\n" * 8, run_id="run_busy"
                    ),
                    app.app_context,
                )
                presenter.handle_steer_receipt(
                    SteerReceipt(
                        str(i), "run_busy", _steer_control(i + 1, run_id="run_busy")
                    ),
                    app.app_context,
                )
            app.prompt.buffer.text = "draft\n" * 8
            lines = _screen_lines(_render_chat_layout(app), output.columns)
            assert any("• 3 steers pending" in line for line in lines)
            assert any("3 queued" in line for line in lines)
            assert any("draft" in line for line in lines)
            assert app.app.layout.current_control is original_focus

    asyncio.run(exercise())


@pytest.mark.parametrize("queued_effort", ["auto", "high"])
def test_chat_queued_root_context_survives_new_defaults_and_run_transition(
    queued_effort: Literal["auto", "high"],
) -> None:
    app = tui.ChatTuiApp(
        thread_id="term_1",
        setting=FakeClient().initial_setting(),
        input_history=None,
        client=FakeClient(),
    )

    def call(name: str, effort: Literal["auto", "low", "high"]) -> QueuedCall:
        return QueuedCall(
            f":agic {name} :model openai/gpt-5 effort={effort} hello",
            RunRequest(
                thread_id="term_1",
                request_id=name,
                runnable=RunnableRequest(f"agic:{name}", CallInput({"_": "hello"})),
                model=ModelRequest(
                    "openai/gpt-5",
                    reasoning=Reasoning(effort=effort) if effort != "auto" else None,
                ),
                policy=RunPolicy(),
            ),
        )

    app.submit_run(call("first", "low"))
    first = next(
        b for b in app.unfinalized_blocks if isinstance(b, blocks.RunControlBlock)
    )
    app.handle_run_event(_run_begin(runnable_name="resolved-first"))
    app.queue.append(call("queued", queued_effort))
    app.setting = SessionSetting(model=ModelRequest("new/default"), runnable="agic:new")
    app.handle_run_event(_run_end(status="succeeded"))
    second = next(
        b for b in app.unfinalized_blocks if isinstance(b, blocks.RunControlBlock)
    )
    assert "agic:resolved-first · openai/gpt-5 · low" in _render_text(first.render())
    assert f"agic:queued · openai/gpt-5 · {queued_effort}" in _render_text(
        second.render()
    )
    app.handle_run_event(_run_begin(run_id="run_next", runnable_name="resolved-queued"))
    assert f"agic:resolved-queued · openai/gpt-5 · {queued_effort}" in _render_text(
        second.render()
    )
    assert "new/default" not in _render_text(first.render()) + _render_text(
        second.render()
    )


@pytest.mark.parametrize("width", [16, 40])
@pytest.mark.parametrize("focused", [False, True])
def test_chat_live_clipping_preserves_the_entire_wrapped_steer_feedback(
    width: int, focused: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, output):
            output.rows = 12
            output.columns = width
            monkeypatch.setattr(rendering, "terminal_width", lambda: width)
            if focused:
                app.app.layout.focus(app.queue_panel.view)
            presenter = app.app_context.get_presenter()
            presenter.add_steer(
                "key",
                blocks.RunSteerBlock.create(message="long\n" * 8, run_id="run_busy"),
                app.app_context,
            )
            presenter.handle_steer_receipt(
                SteerReceipt("key", "run_busy", _steer_control(1, run_id="run_busy")),
                app.app_context,
            )
            app.prompt.buffer.text = "draft\n" * 8
            fragments = "".join(part[1] for part in app._live_fragments())
            feedback = next(
                b
                for b in app.unfinalized_blocks
                if isinstance(b, blocks.SteerFeedbackBlock)
            )
            expected = _render_text(feedback.render(), width=width).strip()
            assert expected in fragments
            lines = _screen_lines(_render_chat_layout(app), width)
            assert not any("Window too small" in line for line in lines)
            assert all(
                any(expected_line in line for line in lines)
                for expected_line in expected.splitlines()
            )

    asyncio.run(exercise())


@pytest.mark.parametrize("width", [1, 2, 3, 4, 5])
def test_chat_extremely_narrow_controls_retain_a_body_cell(
    width: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = _TerminalOutput()
    output.columns = width
    monkeypatch.setattr(
        "toolang.cli.common.input.get_app", lambda: SimpleNamespace(output=output)
    )
    prompt = widgets.PromptBox(lambda _event: None, lambda: None)
    accent, content = prompt.container().children
    assert isinstance(accent, Window) and callable(accent.width)
    assert isinstance(content, HSplit)
    row = content.children[1]
    assert isinstance(row, VSplit)
    left, _, right = row.children
    assert isinstance(left, Window) and callable(left.width)
    assert isinstance(right, Window) and callable(right.width)
    assert (
        width
        - cast(Callable[[], int], accent.width)()
        - cast(Callable[[], int], left.width)()
        - cast(Callable[[], int], right.width)()
        >= 1
    )
    lines = _render_text(
        blocks.RunControlBlock.create("abc").render(), width=width
    ).splitlines()
    assert not lines[0].strip() and not lines[-1].strip()
    assert all(get_cwidth(line) == width for line in lines)
    body = lines[1:-1]
    if width == 1:
        content = "".join(body)
    elif width == 2:
        content = body[0].removeprefix(rendering.CONTROL_BAR_MARK) + "".join(
            line[1:] for line in body[1:]
        )
    else:
        content = "".join(line[2:].strip() for line in body)
    assert content.strip() == "abc"
    if width > 1:
        assert body[0].startswith(rendering.CONTROL_BAR_MARK)


def test_chat_burst_steers_keep_each_draft_independent() -> None:
    async def exercise() -> None:
        with create_app_session(input=DummyInput(), output=DummyOutput()):
            app = tui.ChatTuiApp(
                thread_id="term_busy",
                setting=FakeClient().initial_setting(),
                input_history=None,
                client=FakeClient(),
            )
            app.active_run_id = "run_busy"
            app.app.timeoutlen = None
            try:
                with set_app(app.app):
                    for message in ("first steer", "second steer"):
                        app.prompt.buffer.insert_text(message)
                        for key in (Keys.Escape, Keys.ControlM):
                            app.app.key_processor.feed(KeyPress(key))
                            app.app.key_processor.process_keys()
                    while not app.ui_events.empty():
                        app.handle_ui_event(app.ui_events.get_nowait())
                assert [
                    block.message
                    for block in app.unfinalized_blocks
                    if isinstance(block, blocks.RunSteerBlock)
                ] == ["first steer", "second steer"]
                assert app.prompt.buffer.text == ""
                assert app.prompt.history.get_strings() == [
                    "first steer",
                    "second steer",
                ]
            finally:
                await app.app.cancel_and_wait_for_background_tasks()

    asyncio.run(exercise())


@pytest.mark.parametrize("first_result", ["receipt", "error", "run_end"])
def test_chat_reordered_receipts_preserve_a_consumed_batch_in_history(
    first_result: str,
) -> None:
    app = FakeApp()
    events.handle_run_event(_run_begin(), app)
    first = _submit_test_steer(app, "first", "first steer")
    second = _submit_test_steer(app, "second", "second steer")
    events.handle_run_event(
        replace(
            _model_step_begin(),
            preceded_by=(
                ControlRef.for_run("run_1", 1),
                ControlRef.for_run("run_1", 2),
            ),
        ),
        app,
    )
    app.presenter.handle_steer_receipt(
        SteerReceipt("second", "run_1", _steer_control(2)), app
    )
    assert _steer_feedback(app).strip() == "• 1 steer pending"
    assert not any(b is second for b in app.finalized)
    if first_result == "receipt":
        app.presenter.handle_steer_receipt(
            SteerReceipt("first", "run_1", _steer_control(1)), app
        )
    elif first_result == "error":
        app.presenter.handle_steer_error(
            SteerError("first", "run_1", "receipt lost"), app
        )
    else:
        events.handle_run_event(_run_end(status="canceled"), app)
    assert [id(b) for b in app.finalized if isinstance(b, blocks.RunSteerBlock)] == [
        id(first),
        id(second),
    ]
    assert not first.not_applied and not second.not_applied
    assert not _steer_feedback(app)


@pytest.mark.parametrize("kind", ["run", "steer", "slash"])
def test_chat_tabbed_control_body_keeps_padding_on_every_row(kind: str) -> None:
    message = "abcdef\tghi\tjkl"
    if kind == "run":
        block = blocks.RunControlBlock.create(message)
    elif kind == "steer":
        block = blocks.RunSteerBlock.create(
            message=message, run_id="run_1", max_width=20
        )
    else:
        block = blocks.SlashBlock(message, (), max_width=20)
    lines = _render_text(block.render(), width=20).splitlines()
    body = [line for line in lines if line.strip()]
    assert len(body) == 2
    assert body[0].startswith(f"{rendering.CONTROL_BAR_MARK} ")
    assert all(line.startswith("  ") and line.endswith("  ") for line in body[1:])
    assert all(get_cwidth(line) == 20 for line in body)


@pytest.mark.parametrize("kind", ["agic", "flow"])
def test_chat_status_and_run_context_use_compact_entry_labels(kind: str) -> None:
    from toolang.cli.toolang.commands.chat.blocks import _run_context
    from toolang.cli.toolang.commands.chat.widgets import _chat_runnable_label

    assert _chat_runnable_label(f"agent::{kind}:<entry:3>") == f"{kind}:_"
    assert _chat_runnable_label("agent::agic:<adhoc:5>") == "agic:<adhoc>"
    assert _chat_runnable_label("agic:chat") == "agic:chat"
    assert (
        _run_context(f"agent::{kind}:<entry:3>", "openai/gpt-5", "", 200)
        == f"{kind}:_ · openai/gpt-5"
    )
    status = widgets.StatusBar(f"agent::{kind}:<entry:3>", "openai/gpt-5")
    rendered = "".join(text for _style, text in status._render())
    assert rendered.startswith(f"  {kind}:_ ")
    assert status.runnable_label == f"agent::{kind}:<entry:3>"


@pytest.mark.parametrize("slow_metadata", ["write", "rename"])
def test_chat_terminal_publication_does_not_block_ui_events(slow_metadata: str) -> None:
    from queue import Queue

    from prompt_toolkit.output.vt100 import Vt100_Output

    from toolang.cli.common import tmux
    from toolang.cli.toolang.commands.chat.base import ThreadTitle
    from toolang.cli.toolang.commands.chat.marks import ChatMarks
    from toolang.cli.toolang.commands.chat.title import ChatTitle

    metadata_entered = threading.Event()
    title_entered = threading.Event()
    released = threading.Event()
    results: Queue[ThreadTitle] = Queue()
    writes: list[tuple[str, str]] = []
    stream = StringIO()

    def slow() -> None:
        metadata_entered.set()
        assert released.wait(5)

    def set_window(name: str, value: str) -> None:
        if slow_metadata == "write":
            slow()
        writes.append((name, value))

    def rename_window(name: str) -> None:
        if slow_metadata == "rename":
            slow()
        writes.append(("name", name))

    def lookup(_thread: str) -> str:
        title_entered.set()
        assert released.wait(5)
        return "hello world"

    marks = ChatMarks(
        marks=tmux.Marks(
            pane_id="%3",
            window_id="@1",
            _set_pane=lambda *_: None,
            _set_window=set_window,
            _rename_window=rename_window,
        )
    )

    async def scenario() -> None:
        app = tui.ChatTuiApp(
            thread_id="term_x",
            setting=FakeClient().initial_setting(),
            input_history=None,
            client=FakeClient(),
            marks=marks,
        )
        app.loop = asyncio.get_running_loop()
        app.title = ChatTitle(
            output=Vt100_Output(stream, lambda: Size(24, 80), term="xterm"),
            enabled=True,
            lookup=lookup,
            on_result=results.put,
        )
        app.title.set_thread("term_x")
        marks.start_background()
        marks.start("term_x")
        try:
            app._handle_run_state(RunAccepted("run_1"))
            assert metadata_entered.wait(5)
            assert title_entered.wait(5)
            ui_progress = asyncio.Event()
            app.loop.call_soon(ui_progress.set)
            await ui_progress.wait()
            assert not released.is_set()
            assert "hello world" not in stream.getvalue()
            released.set()
            result = await asyncio.to_thread(results.get, True, 5)
            app.handle_ui_event(ChatUIEvent("thread_title", result))
            assert stream.getvalue().endswith("\x1b]0;hello world\x07")
            app.handle_run_event(RunEnd(run="run_1", status="succeeded"))
            assert results.empty()
        finally:
            released.set()
            app.title.clear()
            marks.clear()
        assert writes[0] == ("@toolang_thread", "term_x")
        assert not any(name == "@toolang_thread_title" for name, _ in writes)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "input_tty, output_tty",
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_terminal_title_requires_tty_but_not_tmux(
    monkeypatch: pytest.MonkeyPatch, input_tty: bool, output_tty: bool
) -> None:
    class Input(DummyInput):
        def fileno(self) -> int:
            return 0

    class Output(DummyOutput):
        def __init__(self) -> None:
            self.writes: list[str] = []

        def fileno(self) -> int:
            return 1

        def write_raw(self, data: str) -> None:
            self.writes.append(data)

    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("TOOLANG_TMUX", "0")
    monkeypatch.setattr(
        tui.os, "isatty", lambda fd: input_tty if fd == 0 else output_tty
    )
    output = Output()
    with create_app_session(input=Input(), output=output):
        app = tui.ChatTuiApp(
            thread_id=None,
            setting=FakeClient().initial_setting(),
            input_history=None,
            client=FakeClient(),
        )
        app.title.start(None)
        app.title.clear()
    assert output.writes == (
        ["\x1b]0;[new chat]\x07", "\x1b]0;\x07"] if input_tty and output_tty else []
    )
    assert not app.marks.active


@pytest.mark.parametrize("fragmented", [False, True])
def test_terminal_focus_reports_preserve_chat_draft_and_status(
    fragmented: bool,
) -> None:
    from prompt_toolkit.input.vt100_parser import Vt100Parser

    async def exercise() -> None:
        app = tui.ChatTuiApp(
            thread_id="term_x",
            setting=FakeClient().initial_setting(),
            input_history=None,
            client=FakeClient(),
        )
        app.app.timeoutlen = None
        app.prompt.buffer.document = Document("keep draft", cursor_position=4)
        app.status_bar.set_error("keep status")
        app.interrupt_exit_pending = True
        parser = Vt100Parser(app.app.key_processor.feed)
        reports = "\x1b[O\x1b[I" * 3
        with set_app(app.app):
            for chunk in reports if fragmented else [reports]:
                parser.feed(chunk)
                app.app.key_processor.process_keys()
            assert app.prompt.buffer.text == "keep draft"
            assert app.prompt.buffer.cursor_position == 4
            assert app.status_bar.error_message == "keep status"
            assert app.interrupt_exit_pending
            assert app.ui_events.empty()
            parser.feed("[O[I")
            app.app.key_processor.process_keys()
            assert app.prompt.buffer.text == "keep[O[I draft"
            await app.app.cancel_and_wait_for_background_tasks()

    asyncio.run(exercise())


def test_chat_tui_seeds_and_updates_session_workdir() -> None:
    class WorkdirClient(FakeClient):
        def initial_workdir(self, thread_id: str | None) -> str:
            assert thread_id == "term_existing"
            return "repo://from-history"

    app = tui.ChatTuiApp(
        thread_id="term_existing",
        setting=WorkdirClient().initial_setting(),
        input_history=None,
        client=WorkdirClient(),
    )

    assert app.setting.workdir == "repo://from-history"
    app._workdir_update_policies["run_1"] = (
        app._session_workdir_revision,
        True,
    )
    app._handle_run_state(RunWorkdirUpdated("run_1", "lab://final"))
    assert app.setting.workdir == "lab://final"
    assert app.setting.workdir_base is None


@pytest.mark.parametrize("queue_state", ["absent", "collapsed", "expanded"])
@pytest.mark.parametrize("running", [False, True])
def test_chat_run_status_immediately_precedes_queue_or_input(
    queue_state: str,
    running: bool,
) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, output):
            if queue_state == "absent":
                app.queue.clear()
            app.queue_panel.expanded = queue_state == "expanded"
            app._set_status_running(running)
            app._status_activity_started_at = 100.0 if running else None
            app._update_status_elapsed(180.9)
            app.prompt.replace_input("draft")
            app.unfinalized_blocks.append(
                blocks.ExecutionProgressBlock(
                    ProgressBlock("step:run_busy.0", (ProgressRow("working"),))
                )
            )
            screen = _render_chat_layout(app)
            lines = _screen_lines(screen, output.columns)
            surface_row = next(
                i
                for i, line in enumerate(lines)
                if ("draft" if queue_state == "absent" else "3 queued") in line
            ) - (1 if queue_state == "absent" else 0)
            assert not lines[surface_row - 2].strip()
            assert lines[surface_row - 1].rstrip() == (
                "  Working for 1m20s" if running else ""
            )
            assert _cell_attrs(app, screen, surface_row - 1, 2).bgcolor == ""
            if running:
                assert _cell_attrs(app, screen, surface_row - 1, 2).dim
            assert "1m20s" not in lines[-1]
            assert "Working" not in lines[-1]
            assert "agic:chat" in lines[-1]
            app._set_status_running(False)
            stopped = _screen_lines(_render_chat_layout(app), output.columns)
            assert len(stopped) == len(lines)
            assert not stopped[surface_row - 1].strip()

    asyncio.run(exercise())


@pytest.mark.parametrize("columns", [1, 2, 4, 5, 10, 80])
@pytest.mark.parametrize(
    "seconds, label",
    [
        (0, "Working"),
        (1, "Working for 1s"),
        (59, "Working for 59s"),
        (60, "Working for 1m0s"),
        (63, "Working for 1m3s"),
        (80, "Working for 1m20s"),
        (3661, "Working for 1h1m1s"),
    ],
)
def test_chat_run_status_fits_compact_elapsed_in_two_rows(
    columns: int,
    seconds: int,
    label: str,
) -> None:
    output = _TerminalOutput()
    output.columns = columns
    with set_app(Application(input=DummyInput(), output=output)):
        status = widgets.RunStatusBar(get_rows=lambda: 2)
        status.set_running(True)
        status.set_elapsed_seconds(seconds)
        lines = "".join(text for _, text in status._render()).split("\n")
        assert len(lines) == 2
        assert lines[0] == ""
        assert get_cwidth(lines[1]) <= columns
        if columns == 80:
            assert lines[1] == f"  {label}"
        status.set_running(False)
        assert not "".join(text for _, text in status._render()).strip()


@pytest.mark.parametrize("rows, status_rows", [(4, 0), (5, 1), (6, 2), (30, 2)])
def test_chat_run_status_yields_space_to_input_on_short_terminals(
    rows: int,
    status_rows: int,
) -> None:
    async def exercise() -> None:
        async with _queue_test_app() as (app, output):
            app.queue.clear()
            output.rows = rows
            app._set_status_running(True)
            app.run_status_bar.set_elapsed_seconds(80)
            app.prompt.replace_input("draft")
            screen = _render_chat_layout(app)
            lines = _screen_lines(screen, output.columns)
            assert screen.height <= rows
            assert app._run_status_rows() == status_rows
            assert any("draft" in line for line in lines)
            assert "agic:chat" in lines[-1]
            assert any("1m20s" in line for line in lines) == bool(status_rows)

    asyncio.run(exercise())


@pytest.mark.parametrize("width", (30, 60))
def test_slash_wrapped_prose_retains_its_inset(width: int) -> None:
    summary = "Runnable set to flow:" + "very_long_name_" * 8
    block = blocks.SlashBlock("/flow review", (summary,), "success")
    lines = _render_text(block.render(), width=width).splitlines()
    start = next(i for i, line in enumerate(lines) if "Runnable" in line)
    assert all(line.startswith("  ") for line in lines[start:] if line.strip())
    assert all(get_cwidth(line) <= width for line in lines)


@pytest.mark.parametrize("width", (1, 2, 3, 4, 30, 60, 100))
@pytest.mark.parametrize("command", ("help", ":?", "keys"))
def test_chat_help_preserves_plain_layout_at_each_width(
    width: int, command: str
) -> None:
    outcome = (
        slashes.run_override_help()
        if command == ":?"
        else slashes.handle(cast(Any, None), QuickCommand(command))
    )
    assert outcome is not None and isinstance(outcome.content, slashes.SlashHelp)
    inset = min(2, width - 1)
    expected = [
        " " * inset + line if line else ""
        for line in slashes.outcome_lines(outcome, width=max(1, width - inset))
    ]
    rendered = _render_text(
        blocks.SlashHelpBlock("?", outcome.content, max_width=width), width=100
    )
    actual = rendered.splitlines()[-len(expected) :]
    assert [line.rstrip() for line in actual] == [line.rstrip() for line in expected]
    assert all(get_cwidth(line) <= width for line in rendered.splitlines())


@pytest.mark.parametrize("width", (30, 60, 100))
def test_chat_table_summary_wraps_with_unicode_and_preserves_inset(width: int) -> None:
    table = slashes.SlashTable(
        "模型 " * 40, ("MODEL",), (("sample *",),), protected_suffixes=(" *",)
    )
    lines = _render_text(
        blocks.SlashTableBlock("/models", table, max_width=width), width=120
    ).splitlines()
    start = next(index for index, line in enumerate(lines) if "模型" in line)
    assert all(line.startswith("  ") for line in lines[start:] if line.strip())
    assert all(get_cwidth(line) <= width for line in lines)
    assert any("sample *" in line for line in lines)


@pytest.mark.parametrize("width", (48, 60))
@pytest.mark.parametrize("rich", (False, True))
def test_keys_help_wraps_descriptions_under_the_description_column(
    width: int, rich: bool
) -> None:
    outcome = slashes.handle(cast(Any, None), QuickCommand("keys"))
    assert outcome is not None
    if rich:
        block = (
            blocks.SlashHelpBlock("/keys", outcome.content, max_width=width)
            if isinstance(outcome.content, slashes.SlashHelp)
            else blocks.SlashBlock(
                "/keys", slashes.outcome_lines(outcome), max_width=width
            )
        )
        lines = _render_text(block, width=100).splitlines()
    else:
        lines = list(slashes.outcome_lines(outcome, width=width))
    start = next(
        i for i, line in enumerate(lines) if line.lstrip().startswith("Ctrl+C ")
    )
    end = next(i for i, line in enumerate(lines) if line.lstrip().startswith("Ctrl+D "))
    description_column = lines[start].index("Clear")
    continuation = lines[start + 1 : end]
    assert continuation
    assert all(
        len(line) - len(line.lstrip()) == description_column for line in continuation
    )
    assert (
        " ".join(
            [
                lines[start][description_column:].strip(),
                *(line.strip() for line in continuation),
            ]
        )
        == shortcuts.INTERRUPT.summary
    )
    assert all(get_cwidth(line) <= width for line in lines)


@pytest.mark.parametrize("streaming", [False, True])
def test_chat_palette_reaches_live_committed_and_durable_output(
    monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    palette = TerminalSurfaces("#102030", "#203040", "#304050", "#405060")
    client = FakeClient()
    app = tui.ChatTuiApp(
        thread_id="thread_1",
        setting=client.initial_setting(),
        input_history=None,
        client=client,
        surfaces=palette,
    )
    context = FakeApp(presenter=app.presenter)
    markup = "before `value` after\n\n```text\nblock\n```"

    def assert_colors(renderable: RenderableType | None) -> None:
        segments = rendering.render_segments(renderable)
        inline = next(segment for segment in segments if segment.text == "value")
        fenced = next(segment for segment in segments if "block" in segment.text)
        assert inline.style is not None
        assert inline.style.bgcolor == Color.parse(palette.inline_code_background)
        assert fenced.style is not None
        assert fenced.style.bgcolor == Color.parse(palette.code_background)

    events.handle_run_event(_run_begin(), context)
    events.handle_run_event(_model_step_begin(), context)
    step = StepRef.parse("run_1.1")
    events.handle_run_event(PartBegin(step=step, part=0, part_type="text"), context)
    if streaming:
        events.handle_run_event(
            PartDelta(step=step, part=0, delta=TextDelta(markup)), context
        )
        assert_colors(
            Group(
                *(
                    rendered
                    for block in [*context.finalized, *context.live_blocks]
                    if (rendered := block.render()) is not None
                )
            )
        )
    events.handle_run_event(PartEnd(step=step, part=0, data=TextPart(markup)), context)
    events.handle_run_event(_model_step_end(output=markup), context)
    events.handle_run_event(_run_end(status="succeeded"), context)
    assert_colors(
        Group(
            *(
                rendered
                for block in context.finalized
                if (rendered := block.render()) is not None
            )
        )
    )

    original = client.get_result("run_saved", thread_id="thread_1")
    monkeypatch.setattr(
        client,
        "get_result",
        lambda run_id, *, thread_id: replace(original, output=(TextPart(markup),)),
    )
    written: list[RenderableType | None] = []
    monkeypatch.setattr(
        tui.rendering,
        "write_renderables",
        lambda renderables, **_kwargs: written.extend(renderables),
    )
    app.handle_submit("/output run_saved")
    assert len(written) == 1
    assert_colors(written[0])


def test_snapshot_cannot_duplicate_a_completed_chat_or_replace_another_run():
    from toolang.execution.events import RunSnapshot

    app = FakeApp()
    presenter = app.presenter
    root = RunBegin("run_one", ControlRef.for_run("run_one", 0))
    end = RunEnd("run_one", "succeeded", finished_at="2026-01-01T00:00:02Z")
    presenter.handle(root, app)
    presenter.handle(end, app)
    finalized = list(app.finalized)
    presenter.handle(RunSnapshot((root, end)), app)
    assert app.finalized == finalized and app.active_run is None
    other = RunBegin("run_two", ControlRef.for_run("run_two", 0))
    presenter.handle(other, app)
    live = list(app.live_blocks)
    presenter.handle(RunSnapshot((root, end)), app)
    assert app.live_blocks == live and app.active_run == "run_two"


def test_snapshot_updates_tui_run_identity_and_completion_title(monkeypatch):
    from toolang.execution.events import RunSnapshot

    app = tui.ChatTuiApp(
        thread_id="term_status",
        setting=FakeClient().initial_setting(),
        input_history=None,
        client=FakeClient(),
    )
    refreshes = []
    monkeypatch.setattr(app.title, "refresh", lambda: refreshes.append(True))
    app.active_run_id = "run_1"
    begin = _run_begin()
    app.handle_run_event(RunSnapshot((begin,)))
    assert app._status_run_id == "run_1"
    app.handle_run_event(RunSnapshot((begin, _run_end(status="succeeded"))))
    assert app.active_run_id is None
    assert refreshes == [True]

    app.active_run_id = "run_other"
    app._status_run_id = "run_other"
    app.handle_run_event(RunSnapshot((begin, _run_end(status="succeeded"))))
    assert app._status_run_id == "run_other"
    assert refreshes == [True]
