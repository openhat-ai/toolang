from __future__ import annotations

import asyncio
from io import StringIO
import re

from toolang.base.types.message import TextPart, ToolResultPart
from toolang.base.types.run import ModelCall, ToolCall
from toolang.cli.common.execution_progress import (
    ProgressBlock,
    ProgressRow,
    ProgressUpdate,
)
from toolang.cli.common.script_progress import ScriptRunPresenter
from toolang.cli.common.script_progress.console import ProgressConsole
from toolang.execution.events import (
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
    assert output.startswith("· Use a shared reducer.\n")
    assert "run_one.0" not in output
    assert "deepseek/deepseek-chat" not in output
    assert "--- run_one succeeded ---" in output
    assert "2.0s · 1 model call · ↑3.4k ↓86 $0.01" in output


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

    assert '· executed web_search.search\n  {\n    "results": [' in output
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

    assert "· thinking…" in output
    assert "\r\x1b[2K" in output


def test_step_error_and_ownerless_run_error_use_dot_rows() -> None:
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

    assert "· failed web_search.search\n  provider returned status 429" in step_error
    assert step_error.count("provider returned status 429") == 1
    assert "!" not in step_error
    assert "· progress stream ended early" in ownerless


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
                            "· executed Alpha beta gamma delta epsilon zeta eta theta"
                        ),
                    ),
                ),
            )
        )
    )
    rendered = re.sub(r"\x1b\[[0-9;]*m", "", stream.getvalue())

    assert rendered.splitlines() == [
        "· executed Alpha beta gamma delta",
        "  epsilon zeta eta theta",
    ]
