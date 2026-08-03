"""Process-local execution for terminal chat sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
import threading
from typing import Any
from uuid import uuid4

from toolang.base.types.message import Message
from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.execution.events import RunEvent, RunTracer
from toolang.execution.calls import bind_runnable_call, validate_setting_commands
from toolang.execution.executor import CeilingSpec, RunExecutor
from toolang.execution.executor.ceiling import (
    agent_model_targets,
    validate_ceiling_spec,
)
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.types import ThreadPrefix
from toolang.lang.includes import resolve_file_include
from toolang.lang.submission import Arguments, SettingCommand, parse_runnable_call
from toolang.setup import SetupWatcher
from toolang.state.state import AgentState
from toolang.state.watcher import StateWatcher
from .base import ChatResult


@dataclass(slots=True)
class _CallbackTracer(RunTracer):
    callback: Callable[[RunEvent], None]

    async def on_event(self, event: RunEvent) -> None:
        self.callback(event)


class LocalChatSession:
    """Expose the chat-client contract over one process-local executor."""

    def __init__(
        self,
        layout: AgentLayout,
        *,
        models: Sequence[str] = (),
        tools: Sequence[str] | None = None,
        caps: Sequence[str] = (),
    ) -> None:
        self.layout = layout
        self.ceiling = CeilingSpec(
            models=tuple(models) or None,
            tools=tuple(tools) if tools is not None else None,
            caps=tuple(caps) or None,
        )
        self.store = RunStore(layout.run_store)
        self.ids = IdIssuer(layout.id_state)
        self.threads = ThreadManager(self.store, self.ids)
        self.executor = RunExecutor(self.store, self.ids)
        self.setup_watcher = SetupWatcher(layout)
        self.state_watcher = StateWatcher(layout)
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop_signal: asyncio.Event | None = None
        self._watch_tasks: tuple[asyncio.Task[None], ...] = ()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._closed = False
        self._thread.start()
        self._ready.wait()
        try:
            self._submit(self._initialize()).result()
        except Exception:
            self.close()
            raise

    def list_models(self) -> Mapping[str, Any]:
        setup = self.setup_watcher.current()
        state = self.state_watcher.current()
        default, targets = agent_model_targets(setup, state, self.ceiling)
        return {
            "default": default,
            "items": [
                {
                    "selector": selector,
                    "name": target.name,
                    "ref": target.ref,
                    "provider": target.provider,
                    "model": target.model,
                    "adapter": target.adapter,
                    "tools": target.tools,
                    "streaming": target.streaming,
                }
                for selector, target in targets
            ],
        }

    def list_executables(self, kind: str) -> Mapping[str, Any]:
        program = self.state_watcher.current().program
        if kind == "agic":
            names = [agic.name for agic in program.agics]
            default = "chat" if program.find_agic("chat") is not None else "default"
            if "default" not in names:
                names.append("default")
            return {
                "default": default,
                "items": [{"name": name} for name in names],
            }
        if kind == "flow":
            return {
                "default": None,
                "items": [{"name": flow.name} for flow in program.flows],
            }
        raise ValueError(f"unknown executable kind: {kind}")

    def create_thread(self) -> str:
        return self.threads.create(prefix=ThreadPrefix.TERM)

    def apply_settings(
        self,
        settings: tuple[SettingCommand, ...],
        selects: Mapping[str, object],
    ) -> Mapping[str, object]:
        candidate = _apply_settings(selects, settings)
        state = self.state_watcher.current()
        setup = self.setup_watcher.current()
        validate_setting_commands(
            _settings(candidate),
            setup=setup,
            state=state,
            ceiling=self.ceiling,
            default_runnable=_default_runnable(state),
        )
        return candidate

    def get_result(
        self,
        run_id: str | None,
        *,
        thread_id: str | None,
    ) -> ChatResult:
        run = self.store.get_run(run_id=run_id) if run_id is not None else None
        if run_id is None:
            if thread_id is None:
                raise ValueError("No run result is available in this chat.")
            run = next(
                (
                    candidate
                    for candidate in reversed(
                        self.store.list_thread_history_chronological(
                            thread_id=thread_id
                        )
                    )
                    if candidate.parent is None
                    and candidate.status == "finished"
                    and candidate.output is not None
                ),
                None,
            )
        if run is None:
            if run_id is not None:
                raise ValueError(f"Run not found: {run_id}")
            raise ValueError("No run result is available in this chat.")
        output = self.store.run_output(run_id=run.id)
        if not output:
            raise ValueError(f"Run has no result: {run.id}")
        return ChatResult(run_id=run.id, output=output)

    def start_run(
        self,
        thread_id: str,
        message: str,
        selects: Mapping[str, object],
        on_event: Callable[[RunEvent], None],
        on_error: Callable[[str], None],
    ) -> None:
        try:
            self._submit(self._run(thread_id, message, selects, on_event)).result()
        except Exception as exc:
            on_error(_error_message(exc))

    def stop_run(
        self,
        run_id: str,
        on_error: Callable[[str], None],
    ) -> None:
        try:
            self.executor.stop(
                run_id=run_id,
                request_id=f"term_{uuid4().hex}",
            )
        except Exception as exc:
            on_error(_error_message(exc))

    def steer_run(
        self,
        run_id: str,
        message: str,
        on_error: Callable[[str], None],
    ) -> None:
        try:
            self.executor.steer(
                run_id=run_id,
                message=Message.user(message),
                timing="next_step",
                request_id=f"term_{uuid4().hex}",
            )
        except Exception as exc:
            on_error(_error_message(exc))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._thread.is_alive() and self._ready.is_set():
            self._submit(self._close(), allow_closed=True).result()
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
        self.store.close()

    async def _initialize(self) -> None:
        state = await self.state_watcher.refresh()
        setup = await self.setup_watcher.refresh()
        validate_ceiling_spec(setup, state, self.ceiling)
        if self._stop_signal is None:
            raise RuntimeError("local chat event loop was not initialized")
        self._watch_tasks = (
            asyncio.create_task(
                self.state_watcher.run(stop_signal=self._stop_signal),
                name=f"toolang-chat-state-{self.layout.name}",
            ),
            asyncio.create_task(
                self.setup_watcher.run(stop_signal=self._stop_signal),
                name=f"toolang-chat-setup-{self.layout.name}",
            ),
        )

    async def _run(
        self,
        thread_id: str,
        message: str,
        selects: Mapping[str, object],
        on_event: Callable[[RunEvent], None],
    ) -> None:
        state = self.state_watcher.current()
        setup = self.setup_watcher.current()
        call = parse_runnable_call(message)
        base = (
            setup.environment.working_directory
            if setup.environment is not None
            else self.layout.home
        )
        spec = bind_runnable_call(
            call,
            setup=setup,
            state=state,
            ceiling=self.ceiling,
            thread=thread_id,
            default_runnable=_default_runnable(state),
            settings=_settings(selects),
            include=lambda reference: resolve_file_include(reference, base=base),
        )
        handle = self.executor.start(
            spec,
            request_id=f"term_{uuid4().hex}",
            tracer=_CallbackTracer(on_event),
        )
        await handle

    async def _close(self) -> None:
        if self._stop_signal is not None:
            self._stop_signal.set()
        await self.executor.shutdown()
        if self._watch_tasks:
            for task in self._watch_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._watch_tasks, return_exceptions=True)

    def _submit(self, coroutine: Any, *, allow_closed: bool = False) -> Future[Any]:
        if self._closed and not allow_closed:
            raise RuntimeError("local chat session is closed")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._stop_signal = asyncio.Event()
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            _close_event_loop(self._loop)
            asyncio.set_event_loop(None)


def _close_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Close an owned event loop after draining every remaining task."""

    pending = {task for task in asyncio.all_tasks(loop) if not task.done()}
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.run_until_complete(loop.shutdown_default_executor())
    loop.close()


def _default_runnable(state: AgentState) -> str:
    return "chat" if state.program.find_agic("chat") is not None else "default"


def _settings(selects: Mapping[str, object]) -> tuple[SettingCommand, ...]:
    result: list[SettingCommand] = []
    model = _text(selects.get("model"))
    if model is not None:
        result.append(SettingCommand(kind="model", selector=model))
    for kind in ("agic", "flow"):
        selector = _text(selects.get(kind))
        if selector is None:
            continue
        args = selects.get("runnable_args")
        result.append(
            SettingCommand(
                kind=kind,
                selector=selector,
                args=_raw_args(args),
            )
        )
    return tuple(result)


def _raw_args(value: object) -> Arguments:
    if not isinstance(value, list | tuple):
        return ()
    result: list[tuple[str, str]] = []
    for item in value:
        if isinstance(item, list | tuple) and len(item) == 2:
            result.append((str(item[0]), str(item[1])))
    return tuple(result)


def _apply_settings(
    selects: Mapping[str, object],
    settings: tuple[SettingCommand, ...],
) -> dict[str, object]:
    result = dict(selects)
    for command in settings:
        if command.kind == "model":
            if command.selector == "default":
                result.pop("model", None)
            else:
                result["model"] = command.selector
            continue
        result.pop("agic", None)
        result.pop("flow", None)
        result.pop("runnable_args", None)
        if command.kind == "agic" and command.selector == "default":
            continue
        result[command.kind] = command.selector
        if command.args:
            result["runnable_args"] = command.args
    return result


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _error_message(exc: Exception) -> str:
    cause = exc.__cause__
    return str(cause or exc) or type(cause or exc).__name__
