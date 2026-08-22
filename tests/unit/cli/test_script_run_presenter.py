from __future__ import annotations

import asyncio
from io import StringIO
import re
from os import terminal_size

import pytest
from rich.live import Live

from toolang.base.types.message import (
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
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

    assert "Thinking..." not in output
    assert output.startswith("\n• Use a shared reducer.\n")
    assert "• Use a shared reducer.\n\n• run_one succeeded" in output
    assert "run_one.0" not in output
    assert "deepseek/deepseek-chat" not in output
    footer = next(
        line
        for line in output.splitlines()
        if line.startswith("• ") and "run_one succeeded" in line
    )
    assert footer.startswith("• run_one succeeded ")
    assert len(footer) == 42
    assert "┌" not in output
    assert "└" not in output
    assert "2.0s · 1 model call · ↑3.4k ↓86 $0.01" in output


@pytest.mark.parametrize("tty", [False, True])
def test_script_tool_call_only_model_step_clears_live_without_scrollback(
    tty: bool,
) -> None:
    stream = _TtyStream() if tty else StringIO()
    presenter = ScriptRunPresenter(run_id="run_one", stream=stream)
    path = StepPath.parse("run_one.0")

    async def scenario() -> None:
        await presenter.on_event(_root_begin())
        await presenter.on_event(StepBegin(step=path, kind="model", given=_model()))
        assert [row.text for row in presenter.console._live_rows] == ["• Thinking..."]

        await presenter.on_event(
            StepEnd(
                step=path,
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

        assert presenter.console._live_rows == []
        presenter.close()

    asyncio.run(scenario())
    output = stream.getvalue()

    assert "requested" not in output
    assert "web_search.search" not in output
    if tty:
        assert "\r\x1b[2K" in output
    else:
        assert output == ""


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

    rendered = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", output)
    assert "• Use a shared reducer.\n" in rendered
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

    assert [line.strip() for line in output.splitlines()][1:6] == [
        "• executed web_search.search",
        "",
        "",
        '{"results":[{},{},{}]}',
        "",
    ]
    assert "run_one.0" not in output


def test_non_tty_tool_surfaces_preserve_tty_block_geometry_without_ansi() -> None:
    stream = StringIO()
    console = ProgressConsole(stream, width=32)
    console.apply(
        ProgressUpdate(
            committed=(
                ProgressBlock(
                    "step:run_one.0",
                    (
                        ProgressRow(
                            "• called read_text README.md",
                            surface="tool_summary",
                        ),
                        ProgressRow("  contents", surface="tool_detail"),
                    ),
                ),
            )
        )
    )

    lines = stream.getvalue().splitlines()
    assert [line.strip() for line in lines] == [
        "• called read_text README.md",
        "",
        "",
        "contents",
        "",
    ]
    assert all(len(line) == 32 for line in lines[2:])
    assert "\x1b[" not in stream.getvalue()
    assert not any(character in stream.getvalue() for character in "│└─┘▏▕▔")


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

    assert "• Thinking..." in output
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

    assert [line.strip() for line in step_error.splitlines()][1:6] == [
        "• failed web_search.search",
        "",
        "",
        "provider returned status 429",
        "",
    ]
    assert step_error.count("provider returned status 429") == 1
    assert "!" not in step_error
    assert "• progress stream ended early" in ownerless


def test_tty_wraps_finalized_model_output_without_adding_a_marker() -> None:
    stream = _TtyStream()
    console = ProgressConsole(stream, width=40)
    console.apply(
        ProgressUpdate(
            committed=(
                ProgressBlock(
                    "step:run_one.0",
                    (
                        ProgressRow(
                            "• executed Alpha beta gamma delta epsilon zeta eta theta"
                        ),
                    ),
                    gap_before=True,
                ),
            )
        )
    )
    rendered = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", stream.getvalue())

    assert rendered.splitlines() == [
        "",
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
            committed=(
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
            committed=(
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


def test_non_tty_prints_only_incrementally_committed_markdown() -> None:
    stream = StringIO()
    console = ProgressConsole(stream, width=40)
    heading = ProgressRow(
        "# Heading\n\n",
        "normal",
        format="markdown",
        prefix="• ",
    )
    paragraph = ProgressRow(
        "Paragraph",
        "normal",
        format="markdown",
        prefix="  ",
        gap_before=True,
    )

    console.apply(ProgressUpdate(live=(ProgressBlock("step:run_one.0", (heading,)),)))
    assert stream.getvalue() == ""

    console.apply(
        ProgressUpdate(
            committed=(ProgressBlock("step:run_one.0", (heading,)),),
            live=(ProgressBlock("step:run_one.0", (paragraph,)),),
        )
    )
    assert stream.getvalue() == "• Heading\n"

    console.apply(
        ProgressUpdate(
            committed=(ProgressBlock("step:run_one.0", (paragraph,)),),
        )
    )
    assert stream.getvalue() == "• Heading\n\n  Paragraph\n"


def test_progress_markdown_uses_a_quiet_unicode_horizontal_rule() -> None:
    stream = StringIO()
    console = ProgressConsole(stream, width=40)
    console.apply(
        ProgressUpdate(
            committed=(
                ProgressBlock(
                    "step:run_one.0",
                    (
                        ProgressRow(
                            "before\n\n---\n\nafter",
                            "normal",
                            format="markdown",
                            prefix="• ",
                        ),
                    ),
                ),
            )
        )
    )

    rendered = stream.getvalue()
    assert "-" not in rendered
    assert "  " + "─" * 38 in rendered
    assert rendered.splitlines() == [
        "• before",
        "",
        "  " + "─" * 38,
        "",
        "  after",
    ]


def test_tty_markdown_separates_inline_and_fenced_code_surfaces() -> None:
    stream = _TtyStream()
    console = ProgressConsole(stream, width=40)
    console.apply(
        ProgressUpdate(
            committed=(
                ProgressBlock(
                    "step:run_one.0",
                    (
                        ProgressRow(
                            "before `value`\n\n```python\nx = 1\n```",
                            "normal",
                            format="markdown",
                            prefix="• ",
                        ),
                    ),
                ),
            )
        )
    )

    rendered = stream.getvalue()
    assert "\x1b[1;36mvalue\x1b[0m" in rendered
    assert "\x1b[100m" in rendered or ";100m" in rendered
    assert "\x1b[38;2" not in rendered
    assert "\x1b[48;2" not in rendered


def test_non_tty_markdown_code_preserves_tty_geometry_without_ansi() -> None:
    stream = StringIO()
    console = ProgressConsole(stream, width=40)
    console.apply(
        ProgressUpdate(
            committed=(
                ProgressBlock(
                    "step:run_one.0",
                    (
                        ProgressRow(
                            "```python\nx = 1\n\n```",
                            "normal",
                            format="markdown",
                            prefix="• ",
                        ),
                    ),
                ),
            )
        )
    )

    lines = stream.getvalue().splitlines()
    assert [line.strip() for line in lines] == ["•", "x = 1", ""]
    assert all(len(line) == 40 for line in lines)


def test_tty_wraps_finalized_parallel_lane_at_its_embedded_marker() -> None:
    stream = _TtyStream()
    console = ProgressConsole(stream, width=40)
    console.apply(
        ProgressUpdate(
            committed=(
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
        ProgressRow("  0 | #0 | • Thinking...", "active"),
    ]

    console.show_live_rows(rows)
    first = stream.getvalue()
    console.show_live_rows(rows)
    second = stream.getvalue()[len(first) :]

    assert first.startswith("\x1b[?25l")
    assert "\x1b[?25h" not in first
    assert "\x1b[?25h" not in second
    assert isinstance(console._live, Live)
    assert console._live.auto_refresh is False
    assert console._live.vertical_overflow == "crop"

    console.close()

    assert "\x1b[?25h" in stream.getvalue()
    assert stream.getvalue().rfind("\x1b[?25h") > stream.getvalue().rfind("\x1b[?25l")


def test_single_run_gather_progressively_commits_markdown() -> None:
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
                delta=TextDelta("# First section\n\n"),
            )
        )
        await presenter.on_event(
            PartDelta(
                step=model,
                part=0,
                delta=TextDelta("Second paragraph"),
            )
        )

        assert len(presenter.console._live_rows) == 1
        assert presenter.console._live_rows[0].text == "Second paragraph"
        assert presenter.console._live_rows[0].format == "markdown"
        assert presenter.console._live_rows[0].prefix == "  "

        await presenter.on_event(
            PartEnd(
                step=model,
                part=0,
                data=TextPart("# First section\n\nSecond paragraph"),
            )
        )
        await presenter.on_event(
            StepEnd(
                step=model,
                kind="model",
                status="succeeded",
                output=_parts("# First section\n\nSecond paragraph"),
            )
        )

        assert presenter.console._live_rows == []

        await presenter.on_event(RunEnd(run="run_merge", status="succeeded"))
        await presenter.on_event(StepEnd(step=gather, kind="run", status="succeeded"))
        await presenter.on_event(RunEnd(run="run_root", status="succeeded"))
        presenter.close()

    asyncio.run(scenario())
    output = stream.getvalue()
    rendered = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", output)

    assert "• First section" in rendered
    assert "  Second paragraph" in rendered


def test_tty_wraps_complete_cjk_output_by_terminal_cell_width() -> None:
    stream = _TtyStream()
    console = ProgressConsole(stream, width=40)
    console.apply(
        ProgressUpdate(
            committed=(
                ProgressBlock(
                    "step:run_one.0",
                    (
                        ProgressRow(
                            "• 已完成对多个来源中的证据和结论的整理并生成最终摘要"
                        ),
                    ),
                    gap_before=True,
                ),
            )
        )
    )
    rendered = re.sub(r"\x1b\[[0-9;]*m", "", stream.getvalue())
    lines = rendered.splitlines()

    assert "".join(line.strip() for line in lines).replace("•", "", 1).strip() == (
        "已完成对多个来源中的证据和结论的整理并生成最终摘要"
    )
    assert lines[0] == ""
    assert all(display_width(line) <= 40 for line in lines)
    assert lines[2].startswith("  ")
