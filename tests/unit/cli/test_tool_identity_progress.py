"""Progress keeps identity and lifecycle independent of persisted descriptions."""

from tests.support.observations import run_begin, run_end, step_begin, step_end

from io import StringIO

import pytest

from toolang.base.types.message import ToolResultPart
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
from toolang.execution.types import (
    ControlRef,
    StepRef,
    ToolStepSpec,
    ToolStepReport,
    Occurrence,
    OccurrencePosition,
    Output,
    value_for_type,
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
    return step_begin(
        step=StepRef.parse(step),
        kind="tool",
        spec=ToolStepSpec(
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
            step_end(
                step=event.step,
                kind="tool",
                status=status,
                report=ToolStepReport("Saved vendor wording"),
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


@pytest.mark.parametrize("name", ["run", "spawn", "await", "exec", "compact", "honor"])
def test_runtime_rows_keep_saved_summaries(name):
    event = begin(f"runtime__{name}", "runtime", summary="Original saved action")
    operation = normalize_operation(event)
    assert operation.name == name
    assert "Original saved action" in trace_live_rows(operation, "")[0].text
    rows = trace_terminal_rows(
        operation,
        step_end(
            step=event.step,
            kind="tool",
            status="succeeded",
            report=ToolStepReport("Original saved outcome"),
        ),
        error="",
    )
    assert "Original saved outcome" in rows[0].text
    assert rows[0].tool_identity == ("runtime", name)
    assert rows[0].tool_status == "succeeded"


def test_former_execute_name_has_no_runtime_presentation():
    operation = normalize_operation(begin("_toolang__execute", "_toolang"))
    assert operation.name == "tool"
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
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="flow:parent",
        )
    )
    par = StepRef.parse("run_root.0")
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
        step_end(
            step=event.step,
            kind="tool",
            status="failed",
            report=ToolStepReport("Failed list: toolang"),
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


def rendered_surfaces(block, width):
    for stream in (StringIO(), Tty()):
        console = ProgressConsole(stream, width=width, max_width=120)
        console.apply(ProgressUpdate(committed=(block,)))
        yield stream.getvalue()
    chat = blocks.ExecutionProgressBlock(block, max_width=120).render()
    yield "".join(
        s.text for s in rendering.render_segments(chat, width=width) if not s.control
    )


@pytest.mark.parametrize("width", [35, 120])
@pytest.mark.parametrize("status", ["succeeded", "failed", "canceled"])
def test_dynamic_run_footer_renders_saved_outcome(status, width):
    projector = ProgressProjector(show_boundaries=False)
    projector.handle(
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="agic:parent",
        )
    )
    event = begin("runtime__run", "runtime")
    projector.handle(event)
    projector.handle(
        run_begin(
            run="run_child",
            control=ControlRef.for_run("run_child", 0),
            runnable="agic:child",
            parent=StepRef.parse(event.step),
        )
    )
    projector.handle(run_end(run="run_child", status=status))
    terminal = projector.handle(
        step_end(
            step=event.step,
            kind="tool",
            status=status,
            report=ToolStepReport("Saved outcome: child"),
        )
    )
    for text in rendered_surfaces(terminal.committed[0], width):
        assert "[runtime]" in text
        assert "Saved outcome:" in text and "child" in text
        assert "run_child" in text and status in text
        assert "└" in text


@pytest.mark.parametrize("target_starts", [True, False])
def test_committed_exec_boundary_keeps_saved_outcome(target_starts):
    projector = ProgressProjector(show_boundaries=False)
    projector.handle(
        run_begin(
            run="run_root",
            control=ControlRef.for_run("run_root", 0),
            runnable="agic:parent",
        )
    )
    event = begin("runtime__exec", "runtime")
    projector.handle(event)
    result = ToolResultPart(
        tool_call_id="call",
        tool_name=event.spec.name,
        tool_family=event.spec.name,
        output={"controls": ["run_root@1"]},
    )
    terminal = projector.handle(
        step_end(
            step=event.step,
            kind="tool",
            status="succeeded",
            output=Output(value_for_type("ToolResultPart", result)),
            report=ToolStepReport("Saved switch: child"),
        )
    )
    assert terminal.committed == ()
    next_event = (
        begin(step="run_root.1")
        if target_starts
        else run_end(run="run_root", status="succeeded")
    )
    boundary = projector.handle(next_event).committed[0]
    assert boundary.rows[0].tool_identity == ("runtime", "exec")
    assert boundary.rows[0].tool_status == "succeeded"
    for text in rendered_surfaces(boundary, 80):
        assert "[runtime] Saved switch: child" in text


def test_narrow_chat_budgets_identity_against_actual_viewport():
    row = trace_live_rows(
        normalize_operation(
            begin("long_configured_toolset_alias__list", summary="Listing: repo")
        ),
        "",
    )[0]
    for text in rendered_surfaces(ProgressBlock("tool", (row,)), 35):
        assert "Listing: repo" in text


def test_trailing_underscore_toolset_identity_reaches_progress():
    row = trace_live_rows(normalize_operation(begin("issues___list")), "")[0]
    assert row.tool_identity == ("issues_", "list")
    for text in rendered_surfaces(ProgressBlock("tool", (row,)), 80):
        assert "[issues_]" in text


@pytest.mark.parametrize(
    ("plugin", "name"),
    [
        ("_toolang", "_toolang__run"),
        ("_toolang", "_toolang__execute"),
        ("_toolang", "runtime__run"),
        ("vendor", "runtime__exec"),
        ("runtime", "_toolang__exec"),
        ("runtime", "runtime__execute"),
        ("runtime", "spawn"),
        ("runtime", "pick"),
        ("runtime", "honor"),
        ("runtime", "compact"),
    ],
)
def test_runtime_progress_uses_only_current_plugin_and_wire_names(plugin, name):
    operation = normalize_operation(begin(name, plugin))
    assert operation.name == "tool"
    assert operation.tool_marker == "›"
    assert not operation.timed
