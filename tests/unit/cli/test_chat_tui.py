from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import threading
from types import SimpleNamespace
from typing import Any, Literal, cast

from prompt_toolkit.output.color_depth import ColorDepth
from rich.console import RenderableType
from rich.text import Text

from toolang.base.types.message import (
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
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
from toolang.cli.common.execution_progress.state import Metrics
from toolang.execution.events import (
    PartDelta,
    RunBegin,
    RunEnd,
    RunEvent,
    StepBegin,
    StepEnd,
)
from toolang.execution.records import RunInputRef, StepOutputRef
from toolang.execution.types import PolicyCommand, StepPath


def test_chat_run_events_keep_run_stop_block_until_run_end() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(), app)
    assert [block.type for block in app.live_blocks] == ["RunStopBlock"]

    events.handle_run_event(_model_step_begin(), app)
    assert [block.type for block in app.live_blocks] == [
        "ModelStepBlock",
        "RunStopBlock",
    ]

    events.handle_run_event(
        _model_step_end(output="final answer", finished_at="2026-01-01T00:00:02Z"),
        app,
    )
    assert [block.type for block in app.live_blocks] == [
        "ModelStepBlock",
        "RunStopBlock",
    ]
    assert app.finalized == []

    events.handle_run_event(_run_end(status="finished"), app)
    assert app.live_blocks == []
    assert [block.type for block in app.finalized] == [
        "ModelStepBlock",
        "RunStopBlock",
    ]
    response = _render_text(app.finalized[0].render())
    assert "· final answer" in response
    assert "  run_1/1 · 1.0s · model · 1/1 tokens" in response
    assert "◆ run_1 succeeded" in _render_text(app.finalized[1].render())
    assert app.finished


def test_chat_run_begin_finalizes_local_submission_block() -> None:
    app = FakeApp()
    app.live_blocks.append(blocks.RunStartBlock.create("hello"))

    assert [block.type for block in app.live_blocks] == ["RunStartBlock"]
    assert "hello" in _render_text(app.live_blocks[0].render())

    events.handle_run_event(_run_begin(), app)

    assert [block.type for block in app.live_blocks] == ["RunStopBlock"]
    assert [block.type for block in app.finalized] == ["RunStartBlock"]
    assert "run_1" in _render_text(app.finalized[0].render())


def test_chat_submission_has_no_status_before_run_begin() -> None:
    block = blocks.RunStartBlock.create("hello")

    rendered = _render_text(block.render(), width=20)

    assert "> hello" in rendered
    assert "starting" not in rendered
    assert rendered.splitlines() == [" " * 20, "> hello" + " " * 13, " " * 20, ""]


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
    assert "! Runnable not found: missing" in rendered
    assert "starting" not in rendered
    assert "run failed" not in rendered
    assert app.finished


def test_chat_next_step_finalizes_local_steer_block() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(), app)
    app.live_blocks.insert(
        0,
        blocks.RunSteerBlock.create(
            message="adjust",
            run_id="run_1",
        ),
    )

    assert [block.type for block in app.live_blocks] == [
        "RunSteerBlock",
        "RunStopBlock",
    ]

    events.handle_run_event(_model_step_begin(), app)

    assert [block.type for block in app.live_blocks] == [
        "ModelStepBlock",
        "RunStopBlock",
    ]
    assert [block.type for block in app.finalized] == ["RunSteerBlock"]
    assert "pending for next step" not in _render_text(app.finalized[0].render())


def test_chat_local_stop_updates_existing_run_stop_block() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(), app)
    stop = cast(blocks.RunStopBlock, app.live_blocks[0])
    stop.mark_canceling()

    assert [block.type for block in app.live_blocks] == ["RunStopBlock"]
    assert "canceling..." in _render_text(app.live_blocks[0].render())


def test_chat_run_stop_block_shows_canceling_then_canceled() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(), app)
    stop = cast(blocks.RunStopBlock, app.live_blocks[0])
    stop.mark_canceling()

    assert [block.type for block in app.live_blocks] == ["RunStopBlock"]
    assert "canceling..." in _render_text(app.live_blocks[0].render())

    events.handle_run_event(_run_end(status="canceled"), app)

    assert app.live_blocks == []
    assert [block.type for block in app.finalized] == ["RunStopBlock"]
    rendered = _render_text(app.finalized[0].render())
    assert rendered.splitlines() == [
        "",
        "◆ run_1 canceled · 3.0s",
        "",
    ]
    assert "---" not in rendered


def test_chat_flow_root_footer_counts_child_runs() -> None:
    block = blocks.RunStopBlock.create(_run_begin(executable_kind="flow"))
    block.update(_run_end(status="finished"))
    block.set_metrics(
        Metrics(
            runs=7,
            model_calls=8,
            tool_calls=2,
            input_tokens=1200,
            output_tokens=300,
        ),
        include_child_runs=True,
    )

    rendered = _render_text(block.render(), width=160)

    assert "run_1 succeeded" in rendered
    assert "6 runs" in rendered
    assert "8 model calls" in rendered
    assert "2 tool calls" in rendered


def test_chat_tool_step_uses_dim_dot_marker_and_summary() -> None:
    block = blocks.ToolStepBlock.create(_tool_step_begin())

    running_segments = [
        segment
        for segment in rendering.render_segments(block.render(), width=80)
        if segment.text.strip()
    ]
    assert running_segments
    assert "· executing tool…" in _render_text(block.render())

    block.update(_tool_step_end())
    rendered = _render_text(block.render())

    assert rendered.startswith("· shell__execute")
    assert "ok" in rendered


def test_chat_canceled_model_step_is_not_rendered_as_completed() -> None:
    block = blocks.ModelStepBlock.create(
        _model_step_begin(model="deepseek/deepseek-chat")
    )
    block.update(
        StepEnd(
            step=StepPath.parse("run_1/1"),
            kind="model",
            status="canceled",
            error="canceled",
            finished_at="2026-01-01T00:00:02Z",
        )
    )

    rendered = _render_text(block.render())

    assert "! model call canceled" in rendered
    assert "  run_1/1 · 1.0s · deepseek/deepseek-chat" in rendered
    assert "model completed" not in rendered


def test_chat_flow_step_blocks_render_flow_operation_summary() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(executable_kind="flow"), app)
    events.handle_run_event(_flow_step_begin(), app)

    assert [block.type for block in app.live_blocks] == [
        "FlowStepBlock",
        "RunStopBlock",
    ]
    assert "[1] statement" in _render_text(app.live_blocks[0].render())
    assert "· starting…" in _render_text(app.live_blocks[0].render())

    events.handle_run_event(_flow_step_end(), app)

    assert [block.type for block in app.live_blocks] == ["RunStopBlock"]
    assert [block.type for block in app.finalized] == ["FlowStepBlock"]
    assert "[1] statement" in _render_text(app.finalized[0].render())
    assert "0 runs · empty input list" in _render_text(app.finalized[0].render())


def test_chat_flow_child_run_events_do_not_finish_parent_run() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(executable_kind="flow"), app)
    events.handle_run_event(_child_run_step_begin(), app)
    events.handle_run_event(_run_begin(run_id="run_child", parent_run_id="run_1"), app)
    events.handle_run_event(_model_step_begin(run_id="run_child"), app)

    assert [block.type for block in app.live_blocks] == [
        "FlowStepBlock",
        "RunStopBlock",
    ]
    rendered = _render_text(app.live_blocks[0].render())
    assert "· thinking…" in rendered
    assert "running…" not in rendered

    events.handle_run_event(
        PartDelta(
            step=StepPath.parse("run_child/1"),
            part=0,
            delta=TextDelta(text="drafting report"),
        ),
        app,
    )

    assert [block.type for block in app.live_blocks] == [
        "FlowStepBlock",
        "RunStopBlock",
    ]
    assert "· drafting report" in _render_text(app.live_blocks[0].render())

    events.handle_run_event(
        _model_step_end(run_id="run_child", output="child done"), app
    )

    assert [block.type for block in app.live_blocks] == [
        "FlowStepBlock",
        "RunStopBlock",
    ]
    assert "· child done" in _render_text(app.live_blocks[0].render())

    events.handle_run_event(_run_end(run_id="run_child", status="finished"), app)

    assert app.active_run == "run_1"
    assert not app.finished
    assert [block.type for block in app.live_blocks] == [
        "FlowStepBlock",
        "RunStopBlock",
    ]
    assert app.finalized == []

    events.handle_run_event(_child_run_step_end(), app)
    statement = _render_text(app.finalized[0].render()).rstrip()
    assert statement.splitlines() == [
        "[2] statement",
        "  Run agic test",
        "  · model completed",
        "    run_child/1 · 1.0s · model · 1/1 tokens",
        "  ↳ run_child succeeded · 3.0s",
    ]
    events.handle_run_event(_run_end(status="finished"), app)

    assert app.live_blocks == []
    assert [block.type for block in app.finalized] == [
        "FlowStepBlock",
        "ResultAvailableBlock",
        "RunStopBlock",
    ]
    assert app.finished


def test_chat_failed_child_is_a_statement_diagnostic_fact() -> None:
    message = "You have no credits remaining."
    app = FakeApp()

    events.handle_run_event(_run_begin(executable_kind="flow"), app)
    events.handle_run_event(
        StepBegin(
            step=StepPath.parse("run_1/1"),
            kind="run",
            given={
                "statement": "scatter",
                "count": 6,
                "runnable": "expand_queries",
                "doc": "Expand the research question into diverse search queries",
                "source": {"head": "scatter 6 expand_queries"},
            },
            started_at="2026-01-01T00:00:01Z",
        ),
        app,
    )
    events.handle_run_event(
        RunBegin(
            run="run_child",
            input=RunInputRef(index=0),
            parent=StepPath.parse("run_1/1"),
            context={
                "origin": "chat",
                "root": "run_1",
                "runnable": {"kind": "agic", "name": "expand_queries"},
            },
            started_at="2026-01-01T00:00:01Z",
        ),
        app,
    )
    events.handle_run_event(_model_step_begin(run_id="run_child"), app)
    events.handle_run_event(
        StepEnd(
            step=StepPath.parse("run_child/1"),
            kind="model",
            status="failed",
            error=message,
            finished_at="2026-01-01T00:00:02Z",
        ),
        app,
    )
    events.handle_run_event(
        RunEnd(
            run="run_child",
            status="failed",
            error=message,
            finished_at="2026-01-01T00:00:02Z",
        ),
        app,
    )
    events.handle_run_event(
        StepEnd(
            step=StepPath.parse("run_1/1"),
            kind="run",
            status="failed",
            error=message,
            finished_at="2026-01-01T00:00:02Z",
        ),
        app,
    )

    assert [block.type for block in app.finalized] == ["FlowStepBlock"]
    rendered = _render_text(app.finalized[0].render())
    assert rendered.count("[1] scatter 6 expand_queries") == 1
    diagnostic = f"! run_1/1 failed: {message}"
    child_fact = "run_child failed · 1.0s"
    assert diagnostic in rendered
    assert child_fact in rendered
    assert rendered.index(diagnostic) < rendered.index(child_fact)
    assert "↳ run_child failed" not in rendered


def test_chat_flow_statement_owns_child_model_tool_model_activity() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(executable_kind="flow"), app)
    events.handle_run_event(_child_run_step_begin(), app)
    events.handle_run_event(_run_begin(run_id="run_child", parent_run_id="run_1"), app)
    events.handle_run_event(_model_step_begin(run_id="run_child", step_index=0), app)
    events.handle_run_event(
        _model_step_end(run_id="run_child", step_index=0, output="call a tool"),
        app,
    )
    events.handle_run_event(
        StepBegin(
            step=StepPath.parse("run_child/1"),
            kind="tool",
            given={"tool": "shell__execute"},
        ),
        app,
    )

    assert [block.type for block in app.live_blocks] == [
        "FlowStepBlock",
        "RunStopBlock",
    ]
    assert "· executing shell__execute…" in _render_text(app.live_blocks[0].render())

    events.handle_run_event(_tool_step_end(run_id="run_child", step_index=1), app)
    events.handle_run_event(_model_step_begin(run_id="run_child", step_index=2), app)

    assert [block.type for block in app.live_blocks] == [
        "FlowStepBlock",
        "RunStopBlock",
    ]
    assert "· thinking…" in _render_text(app.live_blocks[0].render())


def test_late_root_begin_does_not_replace_a_different_active_run() -> None:
    app = FakeApp(active_run="run_new")

    events.handle_run_event(_run_begin(run_id="run_old"), app)

    assert app.active_run == "run_new"


def test_run_event_guard_rejects_unrelated_typed_values() -> None:
    assert not tui._is_run_event(SimpleNamespace(type="run_end"))


def test_chat_nested_step_blocks_are_keyed_by_full_path() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(executable_kind="flow"), app)
    events.handle_run_event(_flow_step_begin(step_index=0), app)
    events.handle_run_event(
        _child_run_step_begin(step=StepPath.parse("run_1/0/0")), app
    )

    assert [block.type for block in app.live_blocks] == [
        "FlowStepBlock",
        "FlowStepBlock",
        "RunStopBlock",
    ]

    events.handle_run_event(_child_run_step_end(step=StepPath.parse("run_1/0/0")), app)

    assert [block.type for block in app.live_blocks] == [
        "FlowStepBlock",
        "RunStopBlock",
    ]
    assert [block.type for block in app.finalized] == ["FlowStepBlock"]


def test_chat_confirms_only_the_root_output_model_response() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(), app)
    events.handle_run_event(_model_step_begin(step_index=0), app)
    events.handle_run_event(_model_step_end(step_index=0, output="internal plan"), app)

    assert "internal plan" in _render_text(app.live_blocks[0].render())
    assert app.finalized == []

    events.handle_run_event(
        StepBegin(
            step=StepPath.parse("run_1/1"),
            kind="tool",
            given={"tool": "shell.execute"},
            started_at="2026-01-01T00:00:02Z",
        ),
        app,
    )

    assert all(
        "internal plan" not in _render_text(block.render()) for block in app.live_blocks
    )
    assert [block.type for block in app.finalized] == ["ModelStepBlock"]
    internal = _render_text(app.finalized[0].render())
    assert "internal plan" not in internal
    assert "model completed" in internal
    assert "run_1/0" in internal

    events.handle_run_event(_tool_step_end(step_index=1), app)
    events.handle_run_event(_model_step_begin(step_index=2), app)
    events.handle_run_event(_model_step_end(step_index=2, output="final answer"), app)

    assert [block.type for block in app.live_blocks] == [
        "ModelStepBlock",
        "RunStopBlock",
    ]

    events.handle_run_event(
        RunEnd(
            run="run_1",
            status="finished",
            output=StepOutputRef(step=StepPath.parse("run_1/2")),
            finished_at="2026-01-01T00:00:04Z",
        ),
        app,
    )

    rendered = "\n".join(_render_text(block.render()) for block in app.finalized)
    assert "internal plan" not in rendered
    assert "final answer" in rendered
    assert "run_1 succeeded" in rendered
    assert [block.type for block in app.finalized] == [
        "ModelStepBlock",
        "ToolStepBlock",
        "ModelStepBlock",
        "RunStopBlock",
    ]


def test_chat_parallel_statement_uses_bounded_zero_based_lanes() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(), app)
    events.handle_run_event(
        StepBegin(
            step=StepPath.parse("run_1/1"),
            kind="par",
            given={
                "statement": "map",
                "runnable": "summarize",
                "par": 2,
                "binding": "_",
                "doc": "Search the web for each query",
                "source": {"head": "map summarize par 2"},
            },
            started_at="2026-01-01T00:00:01Z",
        ),
        app,
    )
    for item in range(2):
        events.handle_run_event(
            RunBegin(
                run=f"run_child_{item}",
                parent=StepPath.parse("run_1/1"),
                input=RunInputRef(),
                context={
                    "root": "run_1",
                    "runnable": {"kind": "agic", "name": "summarize"},
                    "placement": {
                        "item": item,
                        "items": 8,
                        "lane": item,
                        "lanes": 2,
                    },
                },
            ),
            app,
        )
        events.handle_run_event(
            StepBegin(
                step=StepPath.parse(f"run_child_{item}/0"),
                kind="model",
                given={"model": {"ref": "test/model"}},
            ),
            app,
        )

    assert [block.type for block in app.live_blocks] == [
        "FlowStepBlock",
        "RunStopBlock",
    ]
    live = _render_text(app.live_blocks[0].render())
    live_lines = live.splitlines()
    assert live_lines[:5] == [
        "[1] map summarize par 2",
        "  Search the web for each query",
        "",
        "  Run agic summarize in parallel (8 items, 2 lanes)",
        "  · 2 active",
    ]
    assert any(line.startswith("  0 │ item 0") for line in live_lines)
    assert any(line.startswith("  1 │ item 1") for line in live_lines)
    assert "item 2" not in live

    for item in range(2):
        events.handle_run_event(
            StepEnd(
                step=StepPath.parse(f"run_child_{item}/0"),
                kind="model",
                status="finished",
                output=(TextPart(f"summary {item}"),),
            ),
            app,
        )
        events.handle_run_event(
            RunEnd(run=f"run_child_{item}", status="finished"),
            app,
        )
    events.handle_run_event(
        StepEnd(
            step=StepPath.parse("run_1/1"),
            kind="par",
            status="finished",
            noted={"shape": "list", "items": 2},
            finished_at="2026-01-01T00:00:03Z",
        ),
        app,
    )

    stable = _render_text(app.finalized[0].render())
    stable_lines = stable.splitlines()
    assert stable_lines[:5] == [
        "[1] map summarize par 2",
        "  Search the web for each query",
        "",
        "  Run agic summarize in parallel (8 items, 2 lanes)",
        "  · 2 runs succeeded · 2.0s · 2 model calls",
    ]
    assert stable.endswith("\n\n")
    assert "item 0" not in stable
    assert "item 1" not in stable
    assert "2-item list saved to _" in stable


def test_chat_flow_root_result_is_durable_and_hidden_until_requested() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(executable_kind="flow"), app)
    events.handle_run_event(
        StepBegin(
            step=StepPath.parse("run_1/1"),
            kind="run",
            given={
                "statement": "gather",
                "runnable": "synthesize",
                "source": {"head": "gather synthesize"},
            },
        ),
        app,
    )
    events.handle_run_event(
        StepEnd(
            step=StepPath.parse("run_1/1"),
            kind="run",
            status="finished",
            output=(TextPart("flow response"),),
            noted={"shape": "item"},
        ),
        app,
    )
    events.handle_run_event(
        RunEnd(
            run="run_1",
            status="finished",
            output=StepOutputRef(step=StepPath.parse("run_1/1")),
        ),
        app,
    )

    rendered = "\n".join(_render_text(block.render()) for block in app.finalized)
    assert "flow response" not in rendered
    assert "0 runs" in rendered
    assert ":show run_1" in rendered
    assert [block.type for block in app.finalized] == [
        "FlowStepBlock",
        "ResultAvailableBlock",
        "RunStopBlock",
    ]
    result = app.finalized[-2].render()
    result_segments = [
        segment
        for segment in rendering.render_segments(result)
        if "result saved" in segment.text
    ]
    assert result_segments
    assert result_segments[0].style is None or not result_segments[0].style.dim
    assert _render_text(result).startswith("◇ result saved · :show run_1")
    assert "◆ run_1 succeeded" in _render_text(app.finalized[-1].render())


def test_chat_flow_failure_and_cancellation_use_the_same_terminal_summary() -> None:
    failed = FakeApp()
    events.handle_run_event(_run_begin(executable_kind="flow"), failed)
    events.handle_run_event(
        RunEnd(
            run="run_1",
            status="failed",
            error="flow failed",
            finished_at="2026-01-01T00:00:03Z",
        ),
        failed,
    )

    failed_lines = _render_text(failed.finalized[-1].render()).splitlines()
    assert failed_lines[:3] == [
        "! flow failed",
        "",
        "◆ run_1 failed · 3.0s · 0 runs",
    ]
    assert all(block.type != "ResultAvailableBlock" for block in failed.finalized)

    canceled = FakeApp()
    events.handle_run_event(_run_begin(executable_kind="flow"), canceled)
    events.handle_run_event(
        RunEnd(
            run="run_1",
            status="canceled",
            error="canceled",
            finished_at="2026-01-01T00:00:03Z",
        ),
        canceled,
    )

    canceled_lines = _render_text(canceled.finalized[-1].render()).splitlines()
    assert canceled_lines[:2] == [
        "",
        "◆ run_1 canceled · 3.0s · 0 runs",
    ]
    assert all(block.type != "ResultAvailableBlock" for block in canceled.finalized)


def test_chat_canceled_statement_uses_one_diagnostic_and_continuation_facts() -> None:
    block = blocks.FlowStepBlock.create(
        StepBegin(
            step=StepPath.parse("run_1/2"),
            kind="par",
            given={
                "statement": "map",
                "runnable": "search_web",
                "source": {"head": "map search_web par 4"},
            },
            started_at="2026-01-01T00:00:01Z",
        )
    )
    block.state.children.extend(f"run_child_{index}" for index in range(5))
    block.state.completed = 5
    block.state.metrics = Metrics(
        runs=5,
        model_calls=13,
        tool_calls=8,
        input_tokens=13_600,
        output_tokens=3_100,
    )
    block.update(
        StepEnd(
            step=StepPath.parse("run_1/2"),
            kind="par",
            status="canceled",
            error="canceled",
            finished_at="2026-01-01T00:00:28Z",
        )
    )

    rendered = _render_text(block.render())

    assert "  ! run_1/2 canceled" in rendered
    assert "statement failed" not in rendered
    assert "    5 runs succeeded · 27.0s" in rendered
    assert "  · 5 runs succeeded" not in rendered


def test_chat_repeat_keeps_nested_work_in_one_live_block() -> None:
    app = FakeApp()

    events.handle_run_event(_run_begin(), app)
    events.handle_run_event(
        StepBegin(
            step=StepPath.parse("run_1/1"),
            kind="loop",
            given={
                "statement": "repeat",
                "count": 2,
                "source": {"head": "repeat 2"},
            },
            started_at="2026-01-01T00:00:01Z",
        ),
        app,
    )
    events.handle_run_event(
        StepBegin(
            step=StepPath.parse("run_1/1/0"),
            kind="run",
            given={
                "statement": "run",
                "runnable": "revise",
                "placement": {"loop": 0},
                "source": {"head": "run revise"},
            },
        ),
        app,
    )
    events.handle_run_event(
        RunBegin(
            run="run_revise",
            parent=StepPath.parse("run_1/1/0"),
            input=RunInputRef(),
            context={
                "root": "run_1",
                "runnable": {"kind": "agic", "name": "revise"},
                "placement": {"loop": 0},
            },
        ),
        app,
    )
    events.handle_run_event(
        StepBegin(step=StepPath.parse("run_revise/0"), kind="model"),
        app,
    )
    events.handle_run_event(
        PartDelta(
            step=StepPath.parse("run_revise/0"),
            part=0,
            delta=TextDelta(text="revising"),
        ),
        app,
    )

    assert [block.type for block in app.live_blocks] == [
        "FlowStepBlock",
        "RunStopBlock",
    ]
    live = _render_text(app.live_blocks[0].render())
    assert "=== iteration 0 ===" in live
    assert "[0] run revise" in live
    assert "· revising" in live

    events.handle_run_event(
        StepEnd(
            step=StepPath.parse("run_revise/0"),
            kind="model",
            status="finished",
            output=(TextPart("revised"),),
        ),
        app,
    )
    events.handle_run_event(RunEnd(run="run_revise", status="finished"), app)
    events.handle_run_event(
        StepEnd(
            step=StepPath.parse("run_1/1/0"),
            kind="run",
            status="finished",
            output=(TextPart("revised"),),
        ),
        app,
    )
    events.handle_run_event(
        StepEnd(
            step=StepPath.parse("run_1/1"),
            kind="loop",
            status="finished",
            output=(TextPart("revised"),),
            finished_at="2026-01-01T00:00:03Z",
        ),
        app,
    )

    stable = _render_text(app.finalized[0].render())
    assert "[1] repeat 2" in stable
    assert "1 iteration · 2.0s" in stable
    assert "revising" not in stable


def test_chat_reports_a_direct_failure_once() -> None:
    app = FakeApp()
    message = "provider returned status 429"

    events.handle_run_event(_run_begin(), app)
    events.handle_run_event(_tool_step_begin(), app)
    events.handle_run_event(
        StepEnd(
            step=StepPath.parse("run_1/1"),
            kind="tool",
            status="failed",
            error=message,
            finished_at="2026-01-01T00:00:02Z",
        ),
        app,
    )
    events.handle_run_event(
        RunEnd(
            run="run_1",
            status="failed",
            error=message,
            finished_at="2026-01-01T00:00:03Z",
        ),
        app,
    )

    rendered = "\n".join(_render_text(block.render()) for block in app.finalized)
    assert rendered.count(message) == 1
    assert "---" not in rendered
    assert "◆ run_1 failed · 3.0s · 1 tool call" in rendered


def test_chat_command_blocks_render_start_steer_and_stop_states() -> None:
    start = blocks.RunStartBlock.create("hello")
    start.update(_run_begin())
    assert "> hello" in _render_text(start.render())
    assert "run_1" in _render_text(start.render())

    root_summary = blocks.RunStopBlock.create(_run_begin())
    root_summary.update(_run_end(status="failed"))
    marker = next(
        segment
        for segment in rendering.render_segments(root_summary.render(), width=80)
        if "◆" in segment.text
    )
    assert marker.style is not None and marker.style.dim

    steer = blocks.RunSteerBlock.create(
        message="adjust",
        run_id="run_1",
    )
    assert "+ adjust" in _render_text(steer.render())
    assert "pending for next step" in _render_text(steer.render())
    assert not _render_text(steer.render()).splitlines()[0].strip()
    pending_steer_bg = next(
        segment.style.bgcolor
        for segment in rendering.render_segments(steer.render(), width=80)
        if segment.text.startswith("+") and segment.style is not None
    )
    prompt_steer_bg = next(
        fragment[0]
        for fragment in rendering.renderable_to_prompt_toolkit(steer.render())
        if fragment[1].startswith("+")
    )
    start_bg = next(
        segment.style.bgcolor
        for segment in rendering.render_segments(start.render(), width=80)
        if segment.text.startswith(">") and segment.style is not None
    )
    assert pending_steer_bg != start_bg
    assert f"bg:{blocks.STEER_BAR_BG}" in prompt_steer_bg
    steer.update(_model_step_begin(step_index=2))
    assert "pending for next step" not in _render_text(steer.render())
    finalized_steer_bg = next(
        segment.style.bgcolor
        for segment in rendering.render_segments(steer.render(), width=80)
        if segment.text.startswith("+") and segment.style is not None
    )
    assert finalized_steer_bg == pending_steer_bg


def test_chat_model_step_streaming_wraps_like_final_markdown(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(blocks, "markdown_width", lambda: 44)
    long_text = " ".join(f"word{i}" for i in range(40))
    block = blocks.ModelStepBlock.create(_model_step_begin())
    block.update(
        PartDelta(
            step=StepPath.parse("run_1/1"),
            part=0,
            delta=TextDelta(text=long_text),
        )
    )
    live_lines = _render_text(block.render(), width=44).splitlines()

    block.update(_model_step_end(output=long_text))
    final_lines = _render_text(block.render(), width=44).splitlines()

    assert live_lines[0].startswith("· ")
    assert final_lines[0].startswith("· ")
    assert max(len(line) for line in live_lines) <= 44
    assert max(len(line) for line in final_lines) <= 44


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


def test_chat_model_step_marker_style_does_not_leak_to_streaming_text() -> None:
    block = blocks.ModelStepBlock.create(_model_step_begin())
    block.update(
        PartDelta(
            step=StepPath.parse("run_1/1"),
            part=0,
            delta=TextDelta(text="streaming hello"),
        )
    )

    segments = [
        segment
        for segment in rendering.render_segments(block.render(), width=60)
        if segment.text.strip()
    ]
    text = next(segment for segment in segments if "streaming hello" in segment.text)

    assert text.text.startswith("· ")
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
    rendered = _render_text(block.render(), width=80)
    rendered_lines = rendered.splitlines()
    segments = [
        segment
        for segment in rendering.render_segments(block.render(), width=80)
        if segment.text.strip()
    ]

    assert not rendered_lines[0].strip()
    assert rendered_lines[1].startswith("> :?")
    assert not rendered_lines[2].strip()
    assert ": Chat Commands" in rendered
    assert ":model, :models" in rendered
    assert "List or switch models." in rendered
    assert rendered.endswith("\n")
    command = next(segment for segment in segments if segment.text == ":model")
    argument = next(segment for segment in segments if segment.text == ":models")

    assert command.style is not None
    assert command.style.color is not None
    assert argument.style is not None
    assert argument.style.color is not None
    assert all(segment.style is None or not segment.style.bold for segment in segments)


def test_chat_header_shows_resolved_model_label() -> None:
    rendered = _render_text(
        blocks.HeaderBlock(
            model_label="openai/gpt-5",
            home="/tmp/toolang/agents/alice",
            version_label="0.1.0",
        ).render(),
        width=80,
    )

    assert "model: openai/gpt-5" in rendered
    assert "select:" not in rendered
    bordered_lines = [line for line in rendered.splitlines() if line]
    assert len({len(line) for line in bordered_lines}) == 1


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


def test_chat_status_bar_uses_right_aligned_shortcut_hints(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(widgets.StatusBar, "_terminal_width", staticmethod(lambda: 80))
    text = "".join(
        fragment for _style, fragment in widgets.StatusBar("runtime model")._render()
    )

    assert "^c cancel" not in text
    assert "↑↓ history" in text
    assert text.endswith("^d exit  ^j newline  ↑↓ history  ")
    assert len(text) == 80


def test_chat_status_bar_error_uses_full_width_error_line(monkeypatch: Any) -> None:
    monkeypatch.setattr(widgets.StatusBar, "_terminal_width", staticmethod(lambda: 40))
    status = widgets.StatusBar("runtime model")
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


def test_chat_tui_status_bar_uses_resolved_model_and_clears_error_on_input() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        selects={},
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )

    assert app.status_bar.status_label == "openai/gpt-5"
    app.handle_run_event(_model_step_begin(model="deepseek/deepseek-chat"))
    assert app.status_bar.status_label == "deepseek/deepseek-chat"

    app.status_bar.set_error("Model selector matched no models")
    assert app.status_bar.error_message

    app.prompt.buffer.text = "retry"

    assert app.status_bar.error_message == ""


def test_chat_tui_status_bar_hides_only_the_default_agic() -> None:
    app = tui.ChatTuiApp(
        thread_id=None,
        selects={"agic": "default"},
        home="/tmp/agent",
        input_history=None,
        client=FakeClient(),
    )

    assert app.status_bar.status_label == "openai/gpt-5"

    app.selects = {"agic": "review"}
    assert app._status_label() == "openai/gpt-5  agic:review"

    app.selects = {"flow": "research"}
    assert app._status_label() == "openai/gpt-5  flow:research"


def test_chat_default_settings_clear_explicit_model_and_runnable() -> None:
    selects = {
        "model": "openai/o3[openai]",
        "agic": "review",
    }

    updated = apply_session_commands(
        selects,
        (
            PolicyCommand("default", "model", None),
            PolicyCommand("default", "runnable", None),
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
    assert "> hello" in output
    assert "! thread creation failed" in output
    assert "run failed" not in output
    assert app.status_bar.error_message == ""
    assert not app.run_in_flight.is_set()


def test_chat_queue_captures_settings_at_submission_time() -> None:
    class SettingsClient(FakeClient):
        def apply_settings(
            self,
            commands: tuple[PolicyCommand, ...],
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
    block = blocks.FlowStepBlock.create(_flow_step_begin())
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
    assert "thinking…" in "".join(
        _render_text(block.render()) for block in app.unfinalized_blocks
    )
    app._commit_ui_update()

    app.handle_run_event(
        StepEnd(
            step=StepPath.parse("run_1/1"),
            kind="model",
            status="failed",
            error="You have no credits remaining.",
            finished_at="2026-01-01T00:00:02Z",
        )
    )
    app._commit_ui_update()

    assert erases == [False]
    assert len(writes) == 1
    assert "thinking…" not in writes[0]
    assert "You have no credits remaining." in writes[0]
    assert all(
        not isinstance(block, blocks.ModelStepBlock) for block in app.unfinalized_blocks
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

    rendered = "\n".join(_render_text(item) for item in written)
    assert "Result run_saved" in rendered
    assert "durable result" in rendered
    assert _render_text(written[-1]).endswith("\n\n")


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
) -> RunBegin:
    return RunBegin(
        run=run_id,
        input=RunInputRef(index=0),
        parent=(
            StepPath.parse(f"{parent_run_id}/2") if parent_run_id is not None else None
        ),
        started_at="2026-01-01T00:00:00Z",
        context={
            "origin": "chat",
            "root": "run_1",
            "runnable": {"kind": executable_kind, "name": "test"},
        },
    )


def _run_end(
    *,
    run_id: str = "run_1",
    status: Literal["running", "finished", "failed", "canceled"],
) -> RunEnd:
    return RunEnd(
        run=run_id,
        status=status,
        output=(
            StepOutputRef(step=StepPath.parse(f"{run_id}/1"))
            if run_id == "run_1" and status == "finished"
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
        step=StepPath.parse(f"{run_id}/{step_index}"),
        kind="model",
        input=(),
        given={"model": {"ref": model}} if model is not None else {},
        started_at="2026-01-01T00:00:01Z",
    )


def _tool_step_begin(*, step_index: int = 1) -> StepBegin:
    return StepBegin(
        step=StepPath.parse(f"run_1/{step_index}"),
        kind="tool",
        input=(),
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
        step=StepPath.parse(f"{run_id}/{step_index}"),
        kind="model",
        status="finished",
        output=(TextPart(text=output),),
        noted={
            "model_ref": "test/model",
            "tokens": {"input": 1, "output": 1},
        },
        finished_at=finished_at,
    )


def _flow_step_begin(*, step_index: int = 1) -> StepBegin:
    return StepBegin(
        step=StepPath.parse(f"run_1/{step_index}"),
        kind="par",
        input=(),
        started_at="2026-01-01T00:00:01Z",
        given={
            "statement": "map",
            "runnable": "summarize",
            "par": 2,
        },
    )


def _flow_step_end(*, step_index: int = 1) -> StepEnd:
    return StepEnd(
        step=StepPath.parse(f"run_1/{step_index}"),
        kind="par",
        status="finished",
        output=(),
        noted={
            "statement": "map",
            "runnable": "summarize",
            "par": 2,
            "shape": "list",
        },
        finished_at="2026-01-01T00:00:02Z",
    )


def _child_run_step_begin(
    *, step_index: int = 2, step: StepPath | str | None = None
) -> StepBegin:
    return StepBegin(
        step=StepPath.parse(step or f"run_1/{step_index}"),
        kind="run",
        input=(),
        started_at="2026-01-01T00:00:01Z",
        given={
            "statement": "run",
            "runnable": "summarize",
        },
    )


def _child_run_step_end(
    *, step_index: int = 2, step: StepPath | str | None = None
) -> StepEnd:
    return StepEnd(
        step=StepPath.parse(step or f"run_1/{step_index}"),
        kind="run",
        status="finished",
        output=(TextPart(text="done"),),
        noted={
            "statement": "run",
            "runnable": "summarize",
            "shape": "item",
        },
        finished_at="2026-01-01T00:00:02Z",
    )


def _tool_step_end(
    *,
    run_id: str = "run_1",
    step_index: int = 1,
    finished_at: str = "2026-01-01T00:00:02Z",
) -> StepEnd:
    return StepEnd(
        step=StepPath.parse(f"{run_id}/{step_index}"),
        kind="tool",
        status="finished",
        output=(
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
        noted={"tool": "shell__execute"},
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
        del kind
        return {"items": []}

    def create_thread(self) -> str:
        return "thread_1"

    def apply_settings(
        self,
        commands: tuple[PolicyCommand, ...],
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
