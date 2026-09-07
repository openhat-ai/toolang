"""Runtime tool facts reach Script/Chat through ordinary Step events only."""

import asyncio
from io import StringIO
from types import SimpleNamespace

import pytest

from toolang.base.types.message import ToolResultPart
from toolang.base.types.run import ToolCall
from toolang.cli.common.execution_progress import ProgressProjector
from toolang.cli.common.execution_progress.step_projection import (
    trace_live_rows,
    trace_terminal_rows,
)
from toolang.cli.common.script_progress import presenter as script
from toolang.cli.common.script_progress.console import ProgressConsole
from toolang.cli.common.execution_progress import ProgressBlock, ProgressUpdate
from toolang.cli.toolang.commands.chat import blocks, rendering
from toolang.execution.events import RunBegin, RunEnd, StepBegin, StepEnd
from toolang.execution.tools.runtime import runtime_tool_summary
from toolang.execution.types import (
    ControlRef,
    StepRef,
    ToolStepGiven,
    ToolStepNoted,
    Local,
)


START = "2026-01-01T00:00:00Z"
FINISH = "2026-01-01T00:01:20Z"


def _root():
    return RunBegin(
        run="run_root",
        control=ControlRef.for_run("run_root", 0),
        runnable="agent$agic:chat",
        started_at=START,
    )


def _begin(name="compact", arguments=None, files=()):
    arguments = arguments or {}
    summary = runtime_tool_summary(name, arguments, "running", files=files)
    assert summary is not None
    return StepBegin(
        step=StepRef.parse("run_root.0"),
        kind="tool",
        started_at=START,
        given=ToolStepGiven(
            plugin="_toolang",
            trigger="runtime" if name in {"honor", "compact"} else "model",
            call=ToolCall("call-1", "call-1", f"_toolang__{name}", arguments),
            summary=summary,
        ),
    )


def _end(begin, status="succeeded", output=None):
    call = begin.given.call
    summary = runtime_tool_summary(
        call.name.removeprefix("_toolang__"), call.input, status, output=output
    )
    assert summary is not None
    return StepEnd(
        step=begin.step,
        kind="tool",
        status=status,
        finished_at=FINISH,
        noted=ToolStepNoted(summary=summary),
        output=Local.typed(
            "ToolResultPart",
            ToolResultPart(
                call.tool_call_id,
                call.name,
                call.name,
                output=output or {"controls": []},
            ),
            None,
            0,
        ),
    )


@pytest.mark.parametrize(
    "name,arguments,text",
    [
        (
            "pick",
            {"kind": "skill", "ref": "home://skills/testing"},
            "Loaded skill guidance: home://skills/testing",
        ),
        (
            "pick",
            {"kind": "service", "ref": "home://services/github"},
            "Loaded service guidance: home://services/github",
        ),
        ("reload", {}, "Reloaded agent state"),
        ("compact", {}, "Compacted thread history in 1m20s"),
    ],
)
def test_runtime_tools_use_owned_wording_and_progress_marker(name, arguments, text):
    begin = _begin(name, arguments)
    assert trace_live_rows(begin, "")[0].text.startswith("✧ ")
    rows = trace_terminal_rows(begin, _end(begin), error="")
    assert [row.text for row in rows] == [f"✧ {text}"]
    assert rows[0].surface == "tool_summary"


def test_honor_lists_every_rules_file_in_script_and_chat_without_store_reads():
    files = (("repo", "/AGENTS.md"), ("repo", "/src/AGENTS.md"), ("sdk", "/AGENTS.md"))
    begin = _begin(
        "honor", {"paths": [{"workspace": "repo", "path": "/src/file"}]}, files
    )
    output = {
        "controls": [
            {
                "ref": f"run_root@{index}",
                "target": {"kind": "rules", "workspace": workspace, "path": path},
                "revision": "0",
            }
            for index, (workspace, path) in enumerate(files, 1)
        ]
    }
    end = _end(begin, output=output)
    for rows in (trace_live_rows(begin, ""), trace_terminal_rows(begin, end, error="")):
        assert len(rows) == 1
        block = ProgressBlock("honor", rows)
        stream = StringIO()
        ProgressConsole(stream, width=48, max_width=48).apply(
            ProgressUpdate(committed=(block,))
        )
        chat = blocks.ExecutionProgressBlock(block, max_width=48).render()
        rendered_chat = "".join(
            segment.text
            for segment in rendering.render_segments(chat, width=48)
            if not segment.control
        )
        for rendered in (stream.getvalue(), rendered_chat):
            compact = "".join(rendered.split())
            assert all(
                f"workspace://{workspace}{path}" in compact for workspace, path in files
            )
            assert "✧" in rendered


@pytest.mark.parametrize("name", ["pick", "reload", "compact", "honor"])
@pytest.mark.parametrize("status", ["failed", "canceled"])
def test_runtime_tool_failure_details_and_cancellation_remain_visible(name, status):
    begin = _begin(name)
    error = "Resource could not be read" if status == "failed" else ""
    rows = trace_terminal_rows(begin, _end(begin, status), error=error)
    assert rows[0].text.startswith("✧ Failed" if error else "✧ Canceled")
    assert [row.text.strip() for row in rows[1:]] == ([error] if error else [])
    if error:
        assert rows[1].surface == "tool_detail"


@pytest.mark.parametrize(
    "plugin,name", [("_toolang", "run"), ("_toolang", "execute"), ("fs", "read")]
)
def test_other_tool_results_remain_visible(plugin, name):
    call = ToolCall("call-1", "call-1", f"{plugin}__{name}", {})
    begin = StepBegin(
        step=StepRef.parse("run_root.0"),
        kind="tool",
        started_at=START,
        given=ToolStepGiven(
            plugin=plugin,
            call=call,
            summary=f"Executing {name}...",
        ),
    )
    end = StepEnd(
        step=begin.step,
        kind="tool",
        status="succeeded",
        finished_at=FINISH,
        noted=ToolStepNoted(summary=f"Executed {name}"),
        output=Local.typed(
            "ToolResultPart",
            ToolResultPart(
                call.tool_call_id,
                call.name,
                call.name,
                output={"value": "Result is still available"},
            ),
        ),
    )
    rows = trace_terminal_rows(begin, end, error="")
    assert rows[0].text.startswith("• ")
    assert any(
        row.surface == "tool_detail" and "Result is still available" in row.text
        for row in rows
    )


@pytest.mark.parametrize("status", ["succeeded", "failed", "canceled"])
def test_compact_clock_refreshes_without_committing_or_consuming_events(status):
    now = [START]
    projector = ProgressProjector(clock=lambda: now[0])
    projector.handle(_root())
    begin = _begin()
    starting = projector.handle(begin)
    assert "Compacting thread history" in starting.live[0].rows[0].text
    assert projector.has_timed_activity
    now[0] = FINISH
    ticking = projector.refresh()
    assert ticking.committed == ()
    assert ticking.live[0].rows[0].text.endswith("1m20s")
    terminal = projector.handle(_end(begin, status))
    assert any(
        "in 1m20s" in row.text for block in terminal.committed for row in block.rows
    )
    assert not projector.has_timed_activity
    assert projector.refresh().live == ()


class _Tty(StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("stop", ["end", "cancel", "close", "run_end"])
def test_script_reuses_one_refresh_task_and_stops_it(monkeypatch, stop):
    async def scenario():
        now = [START]
        slept = asyncio.Queue()
        ticks = asyncio.Queue()

        async def sleep(delay):
            await slept.put(delay)
            await ticks.get()

        monkeypatch.setattr(
            script,
            "asyncio",
            SimpleNamespace(sleep=sleep, create_task=asyncio.create_task),
        )
        stream = _Tty()
        presenter = script.ScriptRunPresenter(run_id="run_root", stream=stream)
        presenter._projector = ProgressProjector(clock=lambda: now[0])
        await presenter.on_event(_root())
        begin = _begin()
        await presenter.on_event(begin)
        task = presenter._refresh_task
        assert task is not None and await slept.get() == 1
        now[0] = FINISH
        await ticks.put(None)
        assert await slept.get() == 1
        assert presenter._refresh_task is task
        assert "1m20s" in stream.getvalue()
        if stop == "close":
            presenter.close()
        elif stop == "run_end":
            await presenter.on_event(
                RunEnd(run="run_root", status="failed", finished_at=FINISH)
            )
        else:
            await presenter.on_event(
                _end(begin, "canceled" if stop == "cancel" else "succeeded")
            )
        assert presenter._refresh_task is None
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        presenter.close()

    asyncio.run(scenario())


def test_non_tty_prints_compact_start_and_end_without_a_timer():
    async def scenario():
        stream = StringIO()
        presenter = script.ScriptRunPresenter(run_id="run_root", stream=stream)
        await presenter.on_event(_root())
        begin = _begin()
        await presenter.on_event(begin)
        assert "Compacting thread history" in stream.getvalue()
        assert presenter._refresh_task is None
        await presenter.on_event(_end(begin))
        presenter.close()
        assert stream.getvalue().count("Compacting thread history") == 1
        assert stream.getvalue().count("Compacted thread history in 1m20s") == 1

    asyncio.run(scenario())
