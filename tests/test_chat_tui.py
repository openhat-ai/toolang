from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from prompt_toolkit.output.color_depth import ColorDepth
from rich.console import RenderableType

from toolang.base.types.message import (
    Message,
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from toolang.cli.toolang.chat import blocks, events, rendering, slashes, tui, widgets
from toolang.cli.toolang.chat.base import ChatUIEvent
from toolang.execution.events import (
    PartDelta,
    RunBegin,
    RunEnd,
    RunStarting,
    RunSteering,
    RunStopping,
    RunWaiting,
    StepBegin,
    StepEnd,
)
from toolang.execution.records import ModelCallStepPayload, ToolCallStepPayload


def test_chat_trace_events_keep_run_stop_block_until_run_end() -> None:
    app = FakeApp()

    events.handle_trace_event(_run_begin(), app)
    assert [block.type for block in app.live_blocks] == ["RunStopBlock"]

    events.handle_trace_event(_model_step_begin(), app)
    assert [block.type for block in app.live_blocks] == [
        "ModelStepBlock",
        "RunStopBlock",
    ]

    events.handle_trace_event(
        _model_step_end(output="final answer", finished_at="2026-01-01T00:00:02Z"),
        app,
    )
    assert [block.type for block in app.live_blocks] == ["RunStopBlock"]
    assert [block.type for block in app.finalized] == ["ModelStepBlock"]

    events.handle_trace_event(_run_end(status="finished"), app)
    assert app.live_blocks == []
    assert [block.type for block in app.finalized] == [
        "ModelStepBlock",
        "RunStopBlock",
    ]
    assert app.finished


def test_chat_trace_events_finalize_start_block_on_run_begin() -> None:
    app = FakeApp()

    events.handle_trace_event(_run_waiting(), app)
    events.handle_trace_event(_run_starting(), app)

    assert [block.type for block in app.live_blocks] == ["RunStartBlock"]
    assert "hello" in _render_text(app.live_blocks[0].render())

    events.handle_trace_event(_run_begin(), app)

    assert [block.type for block in app.live_blocks] == ["RunStopBlock"]
    assert [block.type for block in app.finalized] == ["RunStartBlock"]
    assert "run_1" in _render_text(app.finalized[0].render())


def test_chat_trace_events_finalize_steer_block_on_next_step() -> None:
    app = FakeApp()

    events.handle_trace_event(_run_begin(), app)
    events.handle_trace_event(_run_steering(), app)

    assert [block.type for block in app.live_blocks] == [
        "RunSteerBlock",
        "RunStopBlock",
    ]

    events.handle_trace_event(_model_step_begin(), app)

    assert [block.type for block in app.live_blocks] == [
        "ModelStepBlock",
        "RunStopBlock",
    ]
    assert [block.type for block in app.finalized] == ["RunSteerBlock"]
    assert "pending for next step" not in _render_text(app.finalized[0].render())


def test_chat_trace_events_update_existing_run_stop_block_on_stop() -> None:
    app = FakeApp()

    events.handle_trace_event(_run_begin(), app)
    events.handle_trace_event(_run_stopping(), app)

    assert [block.type for block in app.live_blocks] == ["RunStopBlock"]
    assert "canceling..." in _render_text(app.live_blocks[0].render())


def test_chat_run_stop_block_shows_canceling_then_canceled() -> None:
    app = FakeApp()

    events.handle_trace_event(_run_begin(), app)
    events.handle_trace_event(_run_stopping(), app)

    assert [block.type for block in app.live_blocks] == ["RunStopBlock"]
    assert "canceling..." in _render_text(app.live_blocks[0].render())

    events.handle_trace_event(_run_end(status="canceled"), app)

    assert app.live_blocks == []
    assert [block.type for block in app.finalized] == ["RunStopBlock"]
    assert "run_1 canceled" in _render_text(app.finalized[0].render())


def test_chat_tool_step_uses_dim_dot_marker_and_summary() -> None:
    block = blocks.ToolStepBlock.create(_tool_step_begin())

    running_segments = [
        segment
        for segment in rendering.render_segments(block.render(), width=80)
        if segment.text.strip()
    ]
    running_marker = next(segment for segment in running_segments if segment.text == "•")

    assert running_marker.style is not None
    assert running_marker.style.dim
    assert "running tool" in _render_text(block.render())

    block.update(_tool_step_end())
    rendered = _render_text(block.render())

    assert rendered.startswith("• ran shell__execute")
    assert "ok" in rendered


def test_chat_command_blocks_render_start_steer_and_stop_states() -> None:
    start = blocks.RunStartBlock.create(
        RunStarting(
            run_id="run_1",
            origin="chat",
            thread_id="thread_1",
            input=Message.user("hello"),
            accepted_at="2026-01-01T00:00:00Z",
        )
    )
    assert "> hello" in _render_text(start.render())
    assert "run_1" in _render_text(start.render())

    steer = blocks.RunSteerBlock.create(
        RunSteering(
            run_id="run_1",
            thread_id="thread_1",
            index=1,
            message=Message.user("adjust"),
            accepted_at="2026-01-01T00:00:01Z",
        )
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
            run_id="run_1",
            thread_id="thread_1",
            step_index=1,
            part_index=0,
            delta=TextDelta(text=long_text),
        )
    )
    live_lines = _render_text(block.render(), width=44).splitlines()

    block.update(_model_step_end(output=long_text))
    final_lines = _render_text(block.render(), width=44).splitlines()

    assert live_lines[0].startswith("• ")
    assert final_lines[0].startswith("• ")
    assert max(len(line) for line in live_lines) <= 44
    assert max(len(line) for line in final_lines) <= 44


def test_chat_model_step_marker_style_does_not_leak_to_streaming_text() -> None:
    block = blocks.ModelStepBlock.create(_model_step_begin())
    block.update(
        PartDelta(
            run_id="run_1",
            thread_id="thread_1",
            step_index=1,
            part_index=0,
            delta=TextDelta(text="streaming hello"),
        )
    )

    segments = [
        segment
        for segment in rendering.render_segments(block.render(), width=60)
        if segment.text.strip()
    ]
    marker = next(segment for segment in segments if segment.text == "•")
    text = next(segment for segment in segments if "streaming hello" in segment.text)

    assert marker.style is not None
    assert marker.style.color is not None
    assert text.style is None or text.style.color is None


def test_chat_slash_block_renders_command_usage_as_table_rows() -> None:
    block = blocks.SlashBlock(
        "/?",
        [
            "Slash Commands",
            "",
            "/help, /?                         Show help.",
            "/model [selector]                List or switch models.",
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
    assert rendered_lines[1].startswith("> /?")
    assert not rendered_lines[2].strip()
    assert ": Slash Commands" in rendered
    assert "/model [selector]" in rendered
    assert "List or switch models." in rendered
    assert rendered.endswith("\n")
    command = next(segment for segment in segments if segment.text == "/model")
    argument = next(segment for segment in segments if segment.text == "[selector]")

    assert command.style is not None
    assert command.style.color is not None
    assert argument.style is not None
    assert argument.style.dim


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
        slashes.chat_model_label(payload, {"models": ["openai/o3[openai]"]})
        == "openai/o3"
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
    block = blocks.SlashBlock("/model", ["Available Models", *slashes._chat_model_list_lines(payload)])
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
    monkeypatch.setattr(
        widgets.StatusBar, "_terminal_width", staticmethod(lambda: 80)
    )
    text = "".join(
        fragment for _style, fragment in widgets.StatusBar("runtime model")._render()
    )

    assert "^c cancel" not in text
    assert "↑↓ history" in text
    assert text.endswith("^d exit  ^j newline  ↑↓ history  ")
    assert len(text) == 80


def test_chat_status_bar_error_uses_full_width_error_line(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        widgets.StatusBar, "_terminal_width", staticmethod(lambda: 40)
    )
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

    app.status_bar.set_error("Model selector matched no models")
    assert app.status_bar.error_message

    app.prompt.buffer.text = "retry"

    assert app.status_bar.error_message == ""


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

    assert block not in app.unfinalized_blocks


def _render_text(renderable: RenderableType | None, *, width: int = 80) -> str:
    return "".join(
        segment.text
        for segment in rendering.render_segments(renderable, width=width)
        if not segment.control
    )


def _run_begin() -> RunBegin:
    return RunBegin(
        run_id="run_1",
        origin="chat",
        thread_id="thread_1",
        input=Message.user("hello"),
        created_at="2026-01-01T00:00:00Z",
        started_at="2026-01-01T00:00:00Z",
    )


def _run_waiting() -> RunWaiting:
    return RunWaiting(
        run_id="run_1",
        origin="chat",
        thread_id="thread_1",
        reason="queue",
        position=1,
        created_at="2026-01-01T00:00:00Z",
    )


def _run_starting() -> RunStarting:
    return RunStarting(
        run_id="run_1",
        origin="chat",
        thread_id="thread_1",
        input=Message.user("hello"),
        accepted_at="2026-01-01T00:00:00Z",
    )


def _run_steering() -> RunSteering:
    return RunSteering(
        run_id="run_1",
        thread_id="thread_1",
        index=1,
        message=Message.user("adjust"),
        accepted_at="2026-01-01T00:00:01Z",
    )


def _run_stopping() -> RunStopping:
    return RunStopping(
        run_id="run_1",
        thread_id="thread_1",
        index=1,
        accepted_at="2026-01-01T00:00:01Z",
    )


def _run_end(*, status: Literal["running", "finished", "failed", "canceled"]) -> RunEnd:
    return RunEnd(
        run_id="run_1",
        thread_id="thread_1",
        status=status,
        finished_at="2026-01-01T00:00:03Z",
    )


def _model_step_begin(*, step_index: int = 1) -> StepBegin:
    return StepBegin(
        run_id="run_1",
        thread_id="thread_1",
        step_index=step_index,
        kind="model",
        input=(),
        started_at="2026-01-01T00:00:01Z",
    )


def _tool_step_begin(*, step_index: int = 1) -> StepBegin:
    return StepBegin(
        run_id="run_1",
        thread_id="thread_1",
        step_index=step_index,
        kind="tool",
        input=(),
        started_at="2026-01-01T00:00:01Z",
    )


def _model_step_end(
    *,
    output: str,
    step_index: int = 1,
    finished_at: str = "2026-01-01T00:00:02Z",
) -> StepEnd:
    return StepEnd(
        run_id="run_1",
        thread_id="thread_1",
        step_index=step_index,
        kind="model",
        status="finished",
        output=(TextPart(text=output),),
        payload=ModelCallStepPayload(
            model_ref="test/model",
            input_tokens=1,
            output_tokens=1,
        ),
        started_at="2026-01-01T00:00:01Z",
        finished_at=finished_at,
    )


def _tool_step_end(
    *,
    step_index: int = 1,
    finished_at: str = "2026-01-01T00:00:02Z",
) -> StepEnd:
    return StepEnd(
        run_id="run_1",
        thread_id="thread_1",
        step_index=step_index,
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
        payload=ToolCallStepPayload(),
        started_at="2026-01-01T00:00:01Z",
        finished_at=finished_at,
    )


@dataclass
class FakeApp:
    live_blocks: list[blocks.MutableBlock] = field(default_factory=list)
    finalized: list[blocks.MutableBlock] = field(default_factory=list)
    active_run: str | None = None
    finished: bool = False

    def get_selects(self) -> dict[str, object]:
        return {}

    def get_client(self) -> Any:
        raise NotImplementedError

    def get_queue(self) -> list[str]:
        return []

    def get_active_run(self) -> str | None:
        return self.active_run

    def set_active_run(self, run_id: str | None) -> None:
        self.active_run = run_id

    def get_live_blocks(self) -> list[blocks.MutableBlock]:
        return self.live_blocks

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


class FakeClient:
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

    def start_run(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def stop_run(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def steer_run(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
