from __future__ import annotations

import asyncio
from io import StringIO
import re
from os import terminal_size

import pytest

from toolang.base.types.message import TextDelta, TextPart, ToolResultPart
from toolang.base.types.run import ModelCall, ToolCall
from toolang.cli.common.execution_progress import (
    ProgressBlock,
    ProgressRow,
    ProgressUpdate,
)
from toolang.cli.common.execution_progress.formatting import display_width
from toolang.cli.common.script_progress import ScriptRunPresenter
from toolang.cli.common.script_progress.console import ProgressConsole
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
    Pointer,
    StepPath,
    ToolStepGiven,
)
from toolang.lang.ast import GatherStmt, Span


class _TtyStream(StringIO):
    def isatty(self) -> bool:
        return True


def _parts(text: str) -> Local:
    return Local.typed("Part[]", (TextPart(text),), "_", 0)


def _model() -> ModelStepGiven:
    return ModelStepGiven(
        model="deepseek/deepseek-chat",
        call=ModelCall(instructions="", messages=[]),
    )


def _tool() -> ToolStepGiven:
    return ToolStepGiven(
        plugin="web_search",
        call=ToolCall(
            tool_call_id="call_1",
            call_id="call_1",
            name="web_search.search",
            input={},
        ),
    )


def _render(events: list[RunEvent], *, tty: bool = False) -> str:
    stream = _TtyStream() if tty else StringIO()
    tracer = ScriptRunPresenter(run_id="run_one", stream=stream)

    async def scenario() -> None:
        for event in events:
            await tracer.on_event(event)
        tracer.close()

    asyncio.run(scenario())
    return stream.getvalue()


def _root_begin() -> RunBegin:
    return RunBegin(
        run="run_one",
        control=ControlRef("run_one", 0),
        runnable="agic:demo",
        started_at="2026-01-01T00:00:00Z",
    )


def test_non_tty_appends_only_finalized_model_progress() -> None:
    output = _render(
        [
            _root_begin(),
            StepBegin(
                step=StepPath.parse("run_one.0"),
                kind="model",
                given=_model(),
                started_at="2026-01-01T00:00:00Z",
            ),
            StepEnd(
                step=StepPath.parse("run_one.0"),
                kind="model",
                status="succeeded",
                output=_parts("Use a shared reducer."),
                noted=ModelStepNoted(
                    tokens=ModelTokenCount(input=3400, output=86),
                    cost="0.006",
                ),
                finished_at="2026-01-01T00:00:01.800Z",
            ),
            RunEnd(
                run="run_one",
                status="succeeded",
                output=Local.typed(
                    "Part[]",
                    Pointer.step(StepPath.parse("run_one.0")),
                    "_",
                    0,
                ),
                finished_at="2026-01-01T00:00:02Z",
            ),
        ]
    )

    assert "thinking" not in output
    assert output.startswith("• Use a shared reducer.\n")
    assert "run_one.0" not in output
    assert "deepseek/deepseek-chat" not in output
    assert "--- run_one succeeded ---" in output
    assert "2.0s · 1 model call · ↑3.4k ↓86 $0.01" in output


def test_tty_model_output_uses_normal_style() -> None:
    output = _render(
        [
            _root_begin(),
            StepBegin(
                step=StepPath.parse("run_one.0"),
                kind="model",
                given=_model(),
            ),
            StepEnd(
                step=StepPath.parse("run_one.0"),
                kind="model",
                status="succeeded",
                output=_parts("Use a shared reducer."),
            ),
        ],
        tty=True,
    )

    assert output.endswith("• Use a shared reducer.\n")
    assert "\x1b[2m• Use a shared reducer." not in output


def test_tool_output_uses_one_unmarked_continuation() -> None:
    output = _render(
        [
            _root_begin(),
            StepBegin(
                step=StepPath.parse("run_one.0"),
                kind="tool",
                given=_tool(),
            ),
            StepEnd(
                step=StepPath.parse("run_one.0"),
                kind="tool",
                status="succeeded",
                output=Local.typed(
                    "Part[]",
                    (
                        ToolResultPart(
                            tool_call_id="call_1",
                            tool_name="web_search.search",
                            tool_family="web_search",
                            output={"results": [{}, {}, {}]},
                        ),
                    ),
                    "_",
                    0,
                ),
            ),
            RunEnd(run="run_one", status="succeeded"),
        ]
    )

    assert '• executed web_search.search\n  {"results":[{},{},{}]}' in output
    assert "run_one.0" not in output


def test_tty_replaces_live_rows_and_clears_them_on_shutdown() -> None:
    output = _render(
        [
            _root_begin(),
            StepBegin(
                step=StepPath.parse("run_one.0"),
                kind="model",
                given=_model(),
            ),
        ],
        tty=True,
    )

    assert "• thinking…" in output
    assert "\r\x1b[2K" in output


def test_step_error_and_ownerless_run_error_use_bullet_rows() -> None:
    step_error = _render(
        [
            _root_begin(),
            StepBegin(
                step=StepPath.parse("run_one.0"),
                kind="tool",
                given=_tool(),
            ),
            StepEnd(
                step=StepPath.parse("run_one.0"),
                kind="tool",
                status="failed",
                error="provider returned status 429",
            ),
            RunEnd(
                run="run_one",
                status="failed",
                error=Pointer.step(StepPath.parse("run_one.0")),
            ),
        ]
    )
    ownerless = _render(
        [
            _root_begin(),
            RunEnd(
                run="run_one",
                status="failed",
                error="progress stream ended early",
            ),
        ]
    )

    assert "• failed web_search.search\n  provider returned status 429" in step_error
    assert step_error.count("provider returned status 429") == 1
    assert "!" not in step_error
    assert "• progress stream ended early" in ownerless


def test_tty_wraps_finalized_model_output_without_adding_a_marker() -> None:
    stream = _TtyStream()
    console = ProgressConsole(stream, width=40)
    console.apply(
        ProgressUpdate(
            finalized=(
                ProgressBlock(
                    "step:run_one.0",
                    (
                        ProgressRow(
                            "• executed Alpha beta gamma delta epsilon zeta eta theta"
                        ),
                    ),
                ),
            )
        )
    )
    rendered = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", stream.getvalue())

    assert rendered.splitlines() == [
        "• executed Alpha beta gamma delta",
        "  epsilon zeta eta theta",
    ]


def test_progress_width_is_bounded_by_maximum_and_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "toolang.cli.common.script_progress.console.shutil.get_terminal_size",
        lambda **_kwargs: terminal_size((240, 24)),
    )
    wide = ProgressConsole(_TtyStream())
    monkeypatch.setattr(
        "toolang.cli.common.script_progress.console.shutil.get_terminal_size",
        lambda **_kwargs: terminal_size((32, 24)),
    )
    narrow = ProgressConsole(_TtyStream(), width=80)

    assert wide.width == 120
    assert narrow.width == 32

    tiny_stream = _TtyStream()
    tiny = ProgressConsole(tiny_stream, width=8)
    tiny.apply(
        ProgressUpdate(
            finalized=(
                ProgressBlock(
                    "par:run_one.0",
                    (ProgressRow("  1 | #5 | • failed provider unavailable"),),
                ),
            )
        )
    )
    rendered = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", tiny_stream.getvalue())

    assert all(display_width(line) <= 8 for line in rendered.splitlines())


def test_progress_width_honors_configured_maximum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "toolang.cli.common.script_progress.console.shutil.get_terminal_size",
        lambda **_kwargs: terminal_size((240, 24)),
    )

    assert ProgressConsole(_TtyStream(), max_width=72).width == 72
    assert ProgressConsole(StringIO(), max_width=72).width == 72


def test_non_tty_finalized_progress_wraps_without_truncation() -> None:
    stream = StringIO()
    console = ProgressConsole(stream)
    content = " ".join(f"word{index}" for index in range(40))

    console.apply(
        ProgressUpdate(
            finalized=(
                ProgressBlock(
                    "step:run_one.0",
                    (ProgressRow(f"• {content}"),),
                ),
            )
        )
    )
    lines = stream.getvalue().splitlines()

    assert all(display_width(line) <= 120 for line in lines)
    assert " ".join(line.strip().removeprefix("• ") for line in lines) == content


def test_tty_wraps_finalized_parallel_lane_at_its_embedded_marker() -> None:
    stream = _TtyStream()
    console = ProgressConsole(stream, width=40)
    console.apply(
        ProgressUpdate(
            finalized=(
                ProgressBlock(
                    "par:run_one.0",
                    (
                        ProgressRow(
                            "  1 | #5 | • failed provider returned a complete "
                            "long diagnostic",
                            "error",
                        ),
                        ProgressRow(
                            "             retry after sixty seconds and contact "
                            "the provider",
                            "error",
                        ),
                    ),
                ),
            )
        )
    )
    rendered = re.sub(r"\x1b\[[0-9;]*m", "", stream.getvalue())

    assert rendered.splitlines() == [
        "  1 | #5 | • failed provider returned a",
        "             complete long diagnostic",
        "             retry after sixty seconds",
        "             and contact the provider",
    ]


def test_tty_keeps_cjk_live_lane_to_one_physical_row() -> None:
    stream = _TtyStream()
    console = ProgressConsole(stream, width=40)
    console.show_live_rows(
        [
            ProgressRow(
                "  0 | #0 | • 正在整理多个来源中的完整证据和结论",
                "active",
            )
        ]
    )
    rendered = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", stream.getvalue())

    assert rendered.count("\n") == 0
    assert display_width(rendered) <= 40
    assert rendered.endswith("…")


def test_tty_hides_cursor_for_the_lifetime_of_parallel_live_output() -> None:
    stream = _TtyStream()
    console = ProgressConsole(stream, width=40)
    rows = [
        ProgressRow("• running · 1 active", "active"),
        ProgressRow("  0 | #0 | • thinking…", "active"),
    ]

    console.show_live_rows(rows)
    first = stream.getvalue()
    console.show_live_rows(rows)
    second = stream.getvalue()[len(first) :]

    assert first.startswith("\x1b[?25l")
    assert "\x1b[?25h" not in first
    assert "\x1b[?25h" not in second

    console.close()

    assert stream.getvalue().endswith("\x1b[?25h")


def test_single_run_gather_accumulates_multiline_model_preview() -> None:
    stream = _TtyStream()
    presenter = ScriptRunPresenter(run_id="run_root", stream=stream, width=40)
    gather = StepPath.parse("run_root.0")
    model = StepPath.parse("run_merge.0")

    async def scenario() -> None:
        await presenter.on_event(
            RunBegin(
                run="run_root",
                control=ControlRef("run_root", 0),
                runnable="flow:summary",
            )
        )
        await presenter.on_event(
            StepBegin(
                step=gather,
                kind="run",
                given=GatherStmt(span=Span(line=1), runnable="merge"),
            )
        )
        await presenter.on_event(
            RunBegin(
                run="run_merge",
                parent=gather,
                control=ControlRef("run_merge", 0),
                runnable="agic:merge",
            )
        )
        await presenter.on_event(StepBegin(step=model, kind="model", given=_model()))
        await presenter.on_event(PartBegin(step=model, part=0, part_type="text"))
        await presenter.on_event(
            PartDelta(
                step=model,
                part=0,
                delta=TextDelta("first streamed line\n"),
            )
        )
        await presenter.on_event(
            PartDelta(
                step=model,
                part=0,
                delta=TextDelta("second streamed line"),
            )
        )

        assert presenter.console._live_lines[-2:] == [
            "• first streamed line",
            "  second streamed line",
        ]

        await presenter.on_event(
            PartEnd(
                step=model,
                part=0,
                data=TextPart("first streamed line\nsecond streamed line"),
            )
        )
        await presenter.on_event(
            StepEnd(
                step=model,
                kind="model",
                status="succeeded",
                output=_parts("first streamed line\nsecond streamed line"),
            )
        )

        assert presenter.console._live_lines == []

        await presenter.on_event(RunEnd(run="run_merge", status="succeeded"))
        await presenter.on_event(StepEnd(step=gather, kind="run", status="succeeded"))
        await presenter.on_event(RunEnd(run="run_root", status="succeeded"))
        presenter.close()

    asyncio.run(scenario())
    output = stream.getvalue()
    after_last_erase = output.rsplit("\x1b[2K", 1)[-1]
    rendered = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", after_last_erase)

    assert "thinking" not in rendered
    assert "• first streamed line\n  second streamed line" in rendered


def test_tty_wraps_complete_cjk_output_by_terminal_cell_width() -> None:
    stream = _TtyStream()
    console = ProgressConsole(stream, width=40)
    console.apply(
        ProgressUpdate(
            finalized=(
                ProgressBlock(
                    "step:run_one.0",
                    (
                        ProgressRow(
                            "• 已完成对多个来源中的证据和结论的整理并生成最终摘要"
                        ),
                    ),
                ),
            )
        )
    )
    rendered = re.sub(r"\x1b\[[0-9;]*m", "", stream.getvalue())
    lines = rendered.splitlines()

    assert "".join(line.strip() for line in lines).replace("•", "", 1).strip() == (
        "已完成对多个来源中的证据和结论的整理并生成最终摘要"
    )
    assert all(display_width(line) <= 40 for line in lines)
    assert lines[1].startswith("  ")
