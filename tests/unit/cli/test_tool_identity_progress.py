"""Progress keeps identity and lifecycle independent of persisted descriptions."""

from io import StringIO

import pytest

from toolang.base.types.run import ToolCall
from toolang.cli.common.execution_progress import (
    ProgressBlock,
    ProgressProjector,
    ProgressUpdate,
)
from toolang.cli.common.execution_progress.operations import normalize_operation
from toolang.cli.common.execution_progress.step_projection import (
    trace_live_rows,
    trace_terminal_rows,
)
from toolang.cli.common.script_progress.console import ProgressConsole
from toolang.cli.toolang.commands.chat import blocks, rendering
from toolang.execution.events import RunBegin, StepBegin, StepEnd
from toolang.execution.types import (
    ControlRef,
    StepRef,
    ToolStepGiven,
    ToolStepNoted,
    Occurrence,
    OccurrencePosition,
)
from toolang.lang.ast import MapStmt, Span


class Tty(StringIO):
    def isatty(self):
        return True


def begin(
    name="issues__list",
    plugin="vendor",
    step="run_root.0",
    summary="Listing projects: toolang",
):
    return StepBegin(
        step=StepRef.parse(step),
        kind="tool",
        given=ToolStepGiven(
            plugin, ToolCall("call", "provider", name, {}), summary=summary
        ),
    )


@pytest.mark.parametrize("status", ["running", "succeeded", "failed", "canceled"])
@pytest.mark.parametrize("width", [35, 120])
def test_alias_identity_and_saved_wording_reach_chat_tty_and_plain(status, width):
    event = begin()
    operation = normalize_operation(event)
    if status == "running":
        rows = trace_live_rows(operation, "")
    else:
        rows = trace_terminal_rows(
            operation,
            StepEnd(
                step=event.step,
                kind="tool",
                status=status,
                noted=ToolStepNoted("Saved vendor wording"),
            ),
            error="detail",
        )
    assert rows[0].tool_identity == ("issues", "list")
    assert rows[0].tool_status == status
    assert "[issues]" not in rows[0].text
    block = ProgressBlock("tool", rows)
    for stream in (StringIO(), Tty()):
        console = ProgressConsole(stream, width=width, max_width=width)
        console.apply(ProgressUpdate(committed=(block,)))
        text = stream.getvalue()
        assert "[issues]" in text and "[vendor]" not in text
        assert "Listing" in text if status == "running" else "Saved vendor" in text
    chat = blocks.ExecutionProgressBlock(block, max_width=width).render()
    text = "".join(
        s.text for s in rendering.render_segments(chat, width=width) if not s.control
    )
    assert "[issues]" in text and "[vendor]" not in text


@pytest.mark.parametrize("namespace", ["runtime", "_toolang"])
@pytest.mark.parametrize("name", ["run", "spawn", "await", "exec", "compact", "honor"])
def test_new_and_historical_runtime_rows_keep_saved_summaries(namespace, name):
    event = begin(f"{namespace}__{name}", namespace, summary="Original saved action")
    operation = normalize_operation(event)
    assert operation.name == name
    assert "Original saved action" in trace_live_rows(operation, "")[0].text
    rows = trace_terminal_rows(
        operation,
        StepEnd(
            step=event.step,
            kind="tool",
            status="succeeded",
            noted=ToolStepNoted("Original saved outcome"),
        ),
        error="",
    )
    assert "Original saved outcome" in rows[0].text
    assert rows[0].tool_identity == (namespace, name)
    assert rows[0].tool_status == "succeeded"


def test_former_execute_name_is_a_history_operation_only():
    operation = normalize_operation(begin("_toolang__execute", "_toolang"))
    assert operation.name == "exec"
    assert operation.tool_identity == ("_toolang", "execute")


@pytest.mark.parametrize("name", ["unknown", "odd__bad__name"])
def test_unknown_identity_is_not_guessed(name):
    row = trace_live_rows(
        normalize_operation(begin(name, "vendor", summary=f"Executing {name}: target")),
        "",
    )[0]
    assert row.tool_identity is None and name in row.text


def test_parallel_activity_and_failed_terminal_keep_identity_and_status():
    projector = ProgressProjector(show_boundaries=False)
    projector.handle(
        RunBegin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="flow:parent",
        )
    )
    par = StepRef.parse("run_root.0")
    projector.handle(
        StepBegin(
            step=par,
            kind="par",
            given=MapStmt(span=Span(line=1), runnable="worker", lanes=1),
        )
    )
    projector.handle(
        RunBegin(
            run="run_worker",
            control=ControlRef.for_run("run_worker", 0),
            parent=par,
            runnable="agic:worker",
            occurrence=Occurrence(
                item=OccurrencePosition(index=0, count=1),
                lane=OccurrencePosition(index=0, count=1),
            ),
        )
    )
    event = begin(step="run_worker.0")
    live = projector.handle(event).live[0].rows[-1]
    assert live.tool_identity == ("issues", "list") and live.tool_status == "running"
    end = projector.handle(
        StepEnd(
            step=event.step,
            kind="tool",
            status="failed",
            noted=ToolStepNoted("Failed list: toolang"),
        )
    )
    terminal = end.live[0].rows[-1]
    assert (
        terminal.tool_identity == ("issues", "list")
        and terminal.tool_status == "failed"
    )
    lane = projector._steps[par].par.lanes[0]
    assert (
        lane.terminal_identity == ("issues", "list")
        and lane.terminal_status == "failed"
    )
