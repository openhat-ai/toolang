"""Terminal chat orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import suppress
import threading
from typing import TypeGuard, cast

import click
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyProcessor
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.containers import DynamicContainer, Float, FloatContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import RenderableType
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
from toolang.execution.runnables import parse_runnable_ref
from toolang.common.errors import ToolangError
from toolang.common.version import toolang_version
from toolang.execution.types import SessionSetting

from toolang.cli.common.execution_progress.config import DEFAULT_MAX_PROGRESS_WIDTH
from . import blocks
from . import events
from . import rendering
from . import slashes
from . import widgets
from .completion import ChatInputCompleter
from .base import (
    AppContext,
    ChatClient,
    ChatRunState,
    QueuedCall,
    RunAccepted,
    RunBlocked,
    RunDisconnected,
    RunRecovered,
    as_text,
    friendly_error,
)
from .events import ChatUIEvent
from .history import ChatInputHistoryStore
from .input import (
    QuickCommand,
    is_runnable_input,
    normalize_chat_input,
    parse_chat_input,
)
from .presenter import ChatRunPresenter

_RUN_EVENT_TYPES = (
    RunBegin,
    StepBegin,
    PartBegin,
    PartDelta,
    PartEnd,
    StepEnd,
    RunEnd,
)
_STATUS_ACTIVITY_TICK = 0.3
_STATUS_SPINNER_FRAME_DURATION = 0.3
_MIN_STATUS_ACTIVITY_DURATION = 0.6
_BLOCKED_READ_ONLY_COMMANDS = frozenset(
    {
        "help",
        "?",
        "model",
        "agic",
        "flow",
        "runnable",
        "show",
        "exit",
        "quit",
    }
)


def _status_spinner_index(elapsed: float) -> int:
    return int(max(0.0, elapsed) / _STATUS_SPINNER_FRAME_DURATION) % len(
        widgets._STATUS_SPINNER_FRAMES
    )


def _selected_runnable(setting: SessionSetting) -> str | None:
    return setting.runnable


def _qualified_runnable_label(reference: str, payload: Mapping[str, object]) -> str:
    try:
        name, kind = parse_runnable_ref(reference)
    except ValueError:
        return reference
    if kind is None:
        items = payload.get("items")
        if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
            matches: set[str | None] = set()
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                values = cast(Mapping[str, object], item)
                if as_text(values.get("name")) == name:
                    matches.add(as_text(values.get("kind")))
            if len(matches) == 1:
                kind = matches.pop()
    return f"{kind}:{name}" if kind in {"agic", "flow"} else reference


class ChatTuiAppContext:
    def __init__(self, app: ChatTuiApp) -> None:
        self._app = app

    def get_setting(self) -> SessionSetting:
        return self._app.setting

    def set_setting(self, setting: SessionSetting) -> None:
        self._app.setting = setting

    def get_client(self) -> ChatClient:
        return self._app.client

    def get_queue(self) -> list[QueuedCall]:
        return self._app.queue

    def get_active_run(self) -> str | None:
        return self._app.active_run_id

    def get_thread_id(self) -> str | None:
        return self._app.thread_id

    def set_active_run(self, run_id: str | None) -> None:
        self._app.active_run_id = run_id

    def get_live_blocks(self) -> list[blocks.MutableBlock]:
        return self._app.unfinalized_blocks

    def get_presenter(self) -> ChatRunPresenter:
        return self._app.presenter

    def ensure_thread_id(self) -> str:
        if self._app.thread_id is None:
            self._app.thread_id = self._app.client.create_thread()
        thread_id = self._app.thread_id
        if thread_id is None:
            raise RuntimeError("failed to create chat thread")
        return thread_id

    def is_busy(self) -> bool:
        return self.get_active_run() is not None or self._app.run_in_flight.is_set()

    def finalize_block(self, block: blocks.MutableBlock) -> None:
        live_blocks = self.get_live_blocks()
        live_blocks[:] = [item for item in live_blocks if item is not block]
        renderable = block.render()
        if self._app.app.is_running:
            self._app._pending_scrollback.append(renderable)
        else:
            rendering.write_renderable(renderable)

    def finish_run(self) -> None:
        self._app._finish_active_run()

    def set_status_error(self, message: str) -> None:
        self._app.status_bar.set_error(message)

    def refresh_status(self) -> None:
        self._app.status_bar.set_status(*self._app._status_labels())
        self._app._refresh_prompt_completions()

    def replace_input(self, text: str) -> None:
        self._app.prompt.replace_input(text)

    def request_steer(self, message: str) -> None:
        self._app._request_run_steer(message)

    def request_exit(self) -> None:
        self._app.app.exit()


class ChatTuiApp:
    @staticmethod
    def run(
        *,
        thread_id: str | None,
        setting: SessionSetting,
        home: str,
        input_history: ChatInputHistoryStore | None,
        client: ChatClient,
        progress_max_width: int = DEFAULT_MAX_PROGRESS_WIDTH,
        resource_paths: tuple[str, ...] = (),
    ) -> None:
        asyncio.run(
            ChatTuiApp(
                thread_id=thread_id,
                setting=setting,
                home=home,
                input_history=input_history,
                client=client,
                progress_max_width=progress_max_width,
                resource_paths=resource_paths,
            ).run_loop()
        )

    def __init__(
        self,
        *,
        thread_id: str | None,
        setting: SessionSetting,
        home: str,
        input_history: ChatInputHistoryStore | None,
        client: ChatClient,
        progress_max_width: int = DEFAULT_MAX_PROGRESS_WIDTH,
        resource_paths: tuple[str, ...] = (),
    ) -> None:
        self.thread_id = thread_id
        self.home = home
        self.input_history = input_history
        self.client = client
        self.setting = setting
        self.ui_events: asyncio.Queue[ChatUIEvent] = asyncio.Queue()
        self.queue: list[QueuedCall] = []
        self.active_run_id: str | None = None
        self.cancel_sent_run_id: str | None = None
        self.submission_blocked: str | None = None
        self.transport_notice: str | None = None
        self.interrupt_exit_pending = False
        self.unfinalized_blocks: list[blocks.MutableBlock] = []
        self._pending_scrollback: list[RenderableType | None] = []
        self.run_in_flight = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.dispatcher_task: asyncio.Task[None] | None = None
        self.status_animation_task: asyncio.Task[None] | None = None
        self._status_animation_wake = asyncio.Event()
        self._status_activity_started_at: float | None = None
        self._status_completed_elapsed_seconds: int | None = None
        self._status_stop_handle: asyncio.TimerHandle | None = None
        self.presenter = ChatRunPresenter(max_width=progress_max_width)
        self.completer = ChatInputCompleter(
            resource_paths=(lambda: list(resource_paths)) if resource_paths else None
        )

        self.queue_panel = widgets.QueuePanel(
            lambda: [item.source for item in self.queue]
        )
        self.status_bar = widgets.StatusBar(*self._status_labels())
        self.prompt = widgets.PromptBox(
            self._enqueue_ui_event,
            self._invalidate_ui,
            on_input=self._clear_status_error,
            history_store=self.input_history,
            completer=self.completer,
        )
        self._refresh_prompt_completions()
        keys = KeyBindings()
        self.prompt.bind(keys)
        body = HSplit(
            [
                DynamicContainer(self._live_blocks_container),
                self.queue_panel.container(),
                self.prompt.container(),
                self.status_bar.container(),
            ]
        )
        self.app = Application(
            layout=Layout(
                FloatContainer(
                    content=body,
                    floats=[
                        Float(
                            xcursor=True,
                            ycursor=True,
                            content=CompletionsMenu(
                                max_height=8,
                                display_arrows=True,
                            ),
                        ),
                    ],
                ),
                focused_element=self.prompt.buffer,
            ),
            key_bindings=keys,
            style=Style.from_dict(widgets._chat_ui_palette()),
            full_screen=False,
            color_depth=ColorDepth.DEPTH_24_BIT,
            erase_when_done=True,
            mouse_support=False,
        )
        self.app.key_processor.after_key_press += self._clear_status_error_on_escape
        self.app_context: AppContext = ChatTuiAppContext(self)

    def _live_blocks_container(self) -> HSplit | Window:
        if not self.unfinalized_blocks:
            return Window(height=0)
        return Window(
            FormattedTextControl(
                lambda: rendering.renderables_to_prompt_toolkit(
                    self._live_renderables(),
                    max_rows=self._available_live_rows(),
                )
            ),
            height=self._live_area_height,
            wrap_lines=False,
            always_hide_cursor=True,
        )

    def _live_renderables(self) -> list[RenderableType | None]:
        return [block.render() for block in self.unfinalized_blocks]

    def _live_area_height(self) -> int:
        return min(
            rendering.renderables_height(self._live_renderables()),
            self._available_live_rows(),
        )

    def _available_live_rows(self) -> int:
        terminal_rows = self.app.output.get_size().rows
        reserved_rows = self.queue_panel.rows() + self.prompt.rows() + 1
        return max(0, terminal_rows - reserved_rows)

    def _enqueue_ui_event(self, event: ChatUIEvent) -> None:
        self.ui_events.put_nowait(event)

    def _refresh_prompt_completions(self) -> None:
        try:
            list_prompts = getattr(self.client, "list_prompts", None)
            if not callable(list_prompts):
                return
            payload = list_prompts(_selected_runnable(self.setting))
            if isinstance(payload, Mapping):
                self.completer.set_prompts(payload)
        except (OSError, RuntimeError, ToolangError, ValueError):
            return

    def _enqueue_ui_event_from_thread(self, event: ChatUIEvent) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.ui_events.put_nowait, event)

    def _invalidate_ui(self) -> None:
        if hasattr(self, "app"):
            self.app.invalidate()

    async def run_loop(self) -> None:
        self.loop = asyncio.get_running_loop()
        rendering.write_renderable(
            blocks.HeaderBlock(
                home=self.home,
                executor_metadata=self.client.executor_metadata,
                version_label=toolang_version(),
            ).render(),
            hide_cursor=False,
        )
        self.dispatcher_task = asyncio.create_task(self._dispatch_ui_events())
        self.status_animation_task = asyncio.create_task(self._animate_status())
        try:
            with patch_stdout(raw=True):
                await self.app.run_async()
        finally:
            self._stop_status_activity()
            if self.status_animation_task is not None:
                self.status_animation_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.status_animation_task
            self.ui_events.put_nowait(ChatUIEvent("quit"))
            if self.dispatcher_task and not self.dispatcher_task.done():
                await self.dispatcher_task

    def _model_label(self) -> str:
        model = self.setting.model
        if model is None:
            return "none"
        reasoning = model.parameters.reasoning
        if reasoning is None:
            return model.ref
        value = (
            reasoning.effort
            if reasoning.effort is not None
            else str(reasoning.budget_tokens)
        )
        return f"{model.ref} · {value}"

    def _runnable_label(
        self,
        setting: SessionSetting | None = None,
    ) -> str:
        current = self.setting if setting is None else setting
        reference = current.runnable
        if reference is None:
            return "none"
        return _qualified_runnable_label(reference, {})

    def _status_labels(self) -> tuple[str, str]:
        return self._runnable_label(), self._model_label()

    def _clear_status_error(self) -> None:
        self.interrupt_exit_pending = False
        if self.transport_notice is not None:
            self.status_bar.set_error(self.transport_notice)
            return
        if self.status_bar.error_message:
            self.status_bar.clear_error()
            self._invalidate_ui()

    def _clear_status_error_on_escape(self, key_processor: KeyProcessor) -> None:
        if key_processor.key_buffer and key_processor.key_buffer[-1].key == Keys.Escape:
            self._clear_status_error()

    async def _animate_status(self) -> None:
        while True:
            await self._status_animation_wake.wait()
            self._status_animation_wake.clear()
            while self.status_bar.running:
                await asyncio.sleep(_STATUS_ACTIVITY_TICK)
                if not self.status_bar.running:
                    break
                loop = self.loop or asyncio.get_running_loop()
                self._update_status_activity(loop.time())

    def _update_status_activity(self, now: float) -> None:
        if self._status_activity_started_at is None:
            return
        elapsed = max(0.0, now - self._status_activity_started_at)
        elapsed_seconds = (
            self._status_completed_elapsed_seconds
            if self._status_completed_elapsed_seconds is not None
            else int(elapsed)
        )
        if self.status_bar.set_activity(
            _status_spinner_index(elapsed), elapsed_seconds
        ):
            self._invalidate_ui()

    def _set_status_running(self, running: bool) -> None:
        if running:
            if self._status_stop_handle is not None:
                self._status_stop_handle.cancel()
                self._status_stop_handle = None
            self._status_activity_started_at = (
                self.loop.time() if self.loop is not None else None
            )
            self._status_completed_elapsed_seconds = None
            self.status_bar.set_running(True)
            self._status_animation_wake.set()
            self._invalidate_ui()
            return
        if (
            self.loop is not None
            and self.loop.is_running()
            and self._status_activity_started_at is not None
        ):
            now = self.loop.time()
            elapsed = max(0.0, now - self._status_activity_started_at)
            self._status_completed_elapsed_seconds = int(elapsed)
            self._update_status_activity(now)
            remaining = _MIN_STATUS_ACTIVITY_DURATION - elapsed
            if remaining > 0:
                if self._status_stop_handle is None:
                    self._status_stop_handle = self.loop.call_later(
                        remaining, self._stop_status_activity
                    )
                return
            self._stop_status_activity()
            return
        self._stop_status_activity()

    def _stop_status_activity(self) -> None:
        if self._status_stop_handle is not None:
            self._status_stop_handle.cancel()
        self._status_stop_handle = None
        self._status_activity_started_at = None
        self._status_completed_elapsed_seconds = None
        self.status_bar.set_running(False)
        self._invalidate_ui()

    async def _dispatch_ui_events(self) -> None:
        while True:
            event = await self.ui_events.get()
            should_exit = False
            try:
                should_exit = self.handle_ui_event(event)
            finally:
                try:
                    self._commit_ui_update()
                finally:
                    self.ui_events.task_done()
            if should_exit:
                return

    def _commit_ui_update(self) -> None:
        pending = self._pending_scrollback.copy()

        if self.app.is_running and pending:
            self.app.renderer.erase(leave_alternate_screen=False)
            self._write_scrollback(pending)
        elif pending:
            rendering.write_renderables(pending)
        del self._pending_scrollback[: len(pending)]
        self.app.invalidate()

    def _write_scrollback(
        self,
        renderables: Sequence[RenderableType | None],
    ) -> None:
        value = rendering.renderables_output(renderables)
        if not value:
            return
        output = self.app.output
        output.hide_cursor()
        try:
            output.write_raw(value)
            output.flush()
        finally:
            output.show_cursor()
            output.flush()

    def handle_ui_event(self, event: ChatUIEvent) -> bool:
        kind = event.type
        if kind == "submit":
            self.handle_submit(str(event.value))
        elif kind == "run_event" and _is_run_event(event.value):
            self.handle_run_event(event.value)
        elif kind == "run_error":
            self._handle_run_error(str(event.value or "run failed"))
        elif kind == "run_state" and _is_run_state(event.value):
            self._handle_run_state(event.value)
        elif kind == "cancel_error":
            self._handle_cancel_error(str(event.value or "cancel request failed"))
        elif kind == "steer_error":
            self._handle_steer_error(str(event.value or "steer request failed"))
        elif kind == "interrupt":
            return self._handle_interrupt()
        elif kind == "eof":
            self._handle_eof()
        elif kind == "cancel":
            self._request_run_cancel()
        elif kind == "clear":
            self._handle_clear()
        elif kind == "quit":
            if self.app.is_running:
                self.app.exit()
            return True
        return False

    def _handle_interrupt(self) -> bool:
        if self.prompt.has_input():
            self.prompt.clear_input()
            self.interrupt_exit_pending = False
            return False
        if self.active_run_id is not None or self.run_in_flight.is_set():
            self.interrupt_exit_pending = False
            self._request_run_cancel()
            return False
        if self.interrupt_exit_pending:
            if self.app.is_running:
                self.app.exit()
            return True
        self.interrupt_exit_pending = True
        self.status_bar.set_error("Press Ctrl-C again to exit.")
        return False

    def _handle_eof(self) -> None:
        if (
            not self.prompt.has_input()
            and self.active_run_id is None
            and not self.run_in_flight.is_set()
        ):
            self.app.exit()

    def _handle_clear(self) -> None:
        if self.active_run_id is not None or self.run_in_flight.is_set():
            self.status_bar.set_error("Cannot clear while a run is active.")
            return
        self.status_bar.clear_error()
        renderer = self.app.renderer
        output = self.app.output
        renderer.erase()
        # Scroll the cleared live origin off the display so it becomes one
        # separator line after the prior terminal history.
        output.write_raw("\r\n" * output.get_size().rows)
        output.erase_screen()
        output.cursor_goto(0, 0)
        output.flush()
        renderer.request_absolute_cursor_position()

    def _finish_active_run(self) -> None:
        self.active_run_id = None
        self.cancel_sent_run_id = None
        self.unfinalized_blocks.clear()
        self.run_in_flight.clear()
        self.transport_notice = None
        self._set_status_running(False)
        self.status_bar.clear_error()
        if self.queue:
            self.submit_run(self.queue.pop(0))

    def handle_submit(self, message: str) -> None:
        self.interrupt_exit_pending = False
        if self.transport_notice is None:
            self.status_bar.clear_error()
        source = normalize_chat_input(message)
        try:
            chat_input = parse_chat_input(source)
        except ValueError as exc:
            self.status_bar.set_error(friendly_error(str(exc)))
            return
        if self.submission_blocked is not None and not _blocked_input_allowed(
            chat_input
        ):
            self.status_bar.set_error(self.submission_blocked)
            return
        if isinstance(chat_input, QuickCommand):
            slash_result = slashes.handle(self.app_context, chat_input)
            if slash_result.result is not None:
                result = slash_result.result
                rendering.write_renderables(
                    [
                        *(
                            [
                                blocks.SlashBlock(
                                    message,
                                    slash_result.lines,
                                ).render()
                            ]
                            if slash_result.lines is not None
                            else []
                        ),
                        blocks.SlashResultBlock(
                            message=message,
                            run_id=result.run_id,
                            parts=result.output,
                        ).render(),
                    ]
                )
                return
            if slash_result.lines is not None:
                rendering.write_renderable(
                    blocks.SlashBlock(message, slash_result.lines).render()
                )
            return
        if not is_runnable_input(chat_input):
            raise AssertionError("unknown chat input value")
        override, runnable_input = chat_input
        try:
            request = self.client.build_request(
                self.app_context.ensure_thread_id(),
                override,
                runnable_input,
                self.setting,
            )
        except click.ClickException as exc:
            self._handle_run_error(exc.message)
            return
        except (ToolangError, ValueError) as exc:
            self._handle_run_error(str(exc))
            return
        queued = QueuedCall(source, request)
        if self.active_run_id is not None or self.run_in_flight.is_set():
            self.queue.append(queued)
        else:
            self.submit_run(queued)

    def _handle_run_error(self, message: str) -> None:
        friendly = friendly_error(message)
        if not events.handle_run_error(self.app_context, friendly):
            self.status_bar.set_error(friendly)

    def _handle_cancel_error(self, message: str) -> None:
        friendly = friendly_error(message)
        if self.active_run_id is None:
            self.status_bar.set_error(friendly)
        else:
            self.status_bar.set_error(f"cancel failed: {friendly}")

    def _handle_steer_error(self, message: str) -> None:
        self.status_bar.set_error(f"steer failed: {friendly_error(message)}")

    def _request_run_cancel(self) -> None:
        if self.submission_blocked is not None:
            self.status_bar.set_error(self.submission_blocked)
            return
        if self.active_run_id is None:
            return
        self.status_bar.clear_error()
        if self.cancel_sent_run_id == self.active_run_id:
            return
        self.cancel_sent_run_id = self.active_run_id
        run_id = self.active_run_id

        def consume() -> None:
            self.client.cancel(
                run_id,
                lambda message: self._enqueue_ui_event_from_thread(
                    ChatUIEvent("cancel_error", message)
                ),
            )

        for block in reversed(self.unfinalized_blocks):
            if isinstance(block, blocks.RunSummaryBlock) and block.run_id == run_id:
                block.mark_canceling()
                break
        threading.Thread(target=consume, daemon=True).start()

    def _request_run_steer(self, message: str) -> None:
        if self.submission_blocked is not None:
            self.status_bar.set_error(self.submission_blocked)
            return
        if self.active_run_id is None:
            self.status_bar.set_error("No active run to steer.")
            return
        self.status_bar.clear_error()
        run_id = self.active_run_id
        self.unfinalized_blocks.insert(
            max(len(self.unfinalized_blocks) - 1, 0),
            blocks.RunSteerBlock.create(
                message=message,
                run_id=run_id,
            ),
        )

        def consume() -> None:
            self.client.steer(
                run_id,
                message,
                lambda error: self._enqueue_ui_event_from_thread(
                    ChatUIEvent("steer_error", error)
                ),
            )

        threading.Thread(target=consume, daemon=True).start()

    def handle_run_event(self, event: RunEvent) -> None:
        if isinstance(event, RunBegin) and event.parent is None and event.runnable:
            self.status_bar.set_active_runnable(event.runnable)
        events.handle_run_event(event, self.app_context)

    def _handle_run_state(self, state: ChatRunState) -> None:
        if isinstance(state, RunAccepted):
            if self.active_run_id not in {None, state.run_id}:
                self.submission_blocked = (
                    "Remote run identity changed. Restart Chat before submitting again."
                )
                self.status_bar.set_error(self.submission_blocked)
                return
            self.active_run_id = state.run_id
            return
        if isinstance(state, RunDisconnected):
            self.active_run_id = state.run_id
            self.transport_notice = state.message
            self.status_bar.set_error(state.message)
            return
        if isinstance(state, RunBlocked):
            if state.run_id is not None:
                self.active_run_id = state.run_id
            self.submission_blocked = state.message
            self.transport_notice = state.message
            self.status_bar.set_error(state.message)
            return
        if isinstance(state, RunRecovered):
            self.transport_notice = None
            events.handle_run_state(state, self.app_context)

    def submit_run(self, call: QueuedCall) -> None:
        self.status_bar.set_active_runnable(call.request.runnable.ref)
        self.unfinalized_blocks.append(blocks.RunControlBlock.create(call.source))
        self.app.invalidate()

        def consume() -> None:
            self.client.run(
                call.request,
                lambda event: self._enqueue_ui_event_from_thread(
                    ChatUIEvent("run_event", event)
                ),
                lambda error: self._enqueue_ui_event_from_thread(
                    ChatUIEvent("run_error", error)
                ),
                lambda state: self._enqueue_ui_event_from_thread(
                    ChatUIEvent("run_state", state)
                ),
            )

        self.run_in_flight.set()
        self._set_status_running(True)
        threading.Thread(target=consume, daemon=True).start()


def _is_run_event(value: object) -> TypeGuard[RunEvent]:
    return isinstance(value, _RUN_EVENT_TYPES)


def _is_run_state(value: object) -> TypeGuard[ChatRunState]:
    return isinstance(
        value,
        (RunAccepted, RunDisconnected, RunRecovered, RunBlocked),
    )


def _blocked_input_allowed(value: object) -> bool:
    if not isinstance(value, QuickCommand):
        return False
    if value.name == "queue":
        return not (value.tail or "").strip()
    if value.name in {"model", "agic", "flow", "runnable"}:
        return value.tail is None
    return value.name in _BLOCKED_READ_ONLY_COMMANDS
