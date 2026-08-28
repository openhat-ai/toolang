"""Process-local execution for terminal chat sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from toolang.base.types.message import Message
from toolang.base.types.progress import ProgressSink
from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.execution.calls import parse_call, validate_session_commands
from toolang.execution.client import LocalRunClient, RunClient
from toolang.execution.events import RunEvent, RunTracer
from toolang.execution.executor import RunExecutor
from toolang.execution.history import RunHistory
from toolang.execution.runnables import runnable_binding_defaults
from toolang.execution.executor.resources import (
    agent_model_targets,
    validate_agent_ceiling,
)
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.schemas import RunRequest
from toolang.execution.types import RunOverride, ThreadPrefix
from toolang.plugin.sandboxes.host import host_sandbox_description
from toolang.setup import SetupWatcher
from toolang.state.watcher import StateWatcher
from toolang.execution.values import parts_from_local
from .base import ChatExecutorMetadata, ChatResult, ChatRunState, RunAccepted
from .policy import apply_session_commands, commands_from_selects


@dataclass(slots=True)
class _CallbackTracer(RunTracer):
    callback: Callable[[RunEvent], None]

    async def on_event(self, event: RunEvent) -> None:
        self.callback(event)


class LocalChatSession:
    """Expose the chat-client contract over one process-local executor."""

    executor_metadata: ChatExecutorMetadata

    def __init__(
        self,
        layout: AgentLayout,
        *,
        sandbox: str = "host",
        model_catalog: Path | None = None,
        ceiling_overrides: Mapping[str, tuple[str, ...] | None] | None = None,
        binding_overrides: Mapping[str, str | None] | None = None,
        limit_overrides: Mapping[str, int | Decimal | None] | None = None,
        progress: ProgressSink | None = None,
    ) -> None:
        self.layout = layout
        self.executor_metadata = ChatExecutorMetadata(
            sandbox_selector="host",
            sandbox_detail=host_sandbox_description(),
        )
        self.store = RunStore(layout.run_store)
        self.history = RunHistory(self.store)
        self.ids = IdIssuer(layout.id_state)
        self.threads = ThreadManager(self.store, self.ids)
        self.setup_watcher = SetupWatcher(
            layout,
            sandbox=sandbox,
            model_catalog=model_catalog,
            ceiling_overrides=ceiling_overrides,
            binding_overrides=binding_overrides,
            limit_overrides=limit_overrides,
        )
        self.state_watcher = StateWatcher(layout)
        self._initial_progress = progress
        self.executor = RunExecutor(
            self.store,
            self.ids,
            setup=self.setup_watcher.current,
            state=self.state_watcher.current,
            load_state=lambda revision: self.state_watcher.load(revision),
            refresh_state=self.state_watcher.refresh_result,
        )
        self.run_client: RunClient = LocalRunClient(self.executor)
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
        default, targets = agent_model_targets(setup, state, setup.ceiling)
        default = setup.bindings.model or default
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

    def list_runnables(self, kind: str) -> Mapping[str, Any]:
        setup = self.setup_watcher.current()
        state = self._submit(self.state_watcher.refresh()).result()
        default_agic, default_flow = runnable_binding_defaults(
            state,
            setup.bindings.runnable,
            fallback_agic="chat",
        )
        if kind == "agic":
            names = list(state.agics)
            default = default_agic
            return {
                "default": default,
                "items": [{"name": name} for name in names],
            }
        if kind == "flow":
            return {
                "default": default_flow,
                "items": [{"name": name} for name in state.flows],
            }
        if kind == "runnable":
            return {
                "default": setup.bindings.runnable or f"agic:{default_agic}",
                "items": [
                    {"kind": item.kind, "name": name}
                    for name, item in state.runnables.items()
                ],
            }
        raise ValueError(f"unknown runnable kind: {kind}")

    def create_thread(self) -> str:
        return self.threads.create(prefix=ThreadPrefix.TERM)

    def apply_settings(
        self,
        commands: tuple[RunOverride, ...],
        selects: Mapping[str, object],
    ) -> Mapping[str, object]:
        candidate = apply_session_commands(selects, commands)
        state = self.state_watcher.current()
        setup = self.setup_watcher.current()
        validate_session_commands(
            commands_from_selects(candidate),
            setup=setup,
            state=state,
            runnable_fallbacks=("agic:chat", "default"),
        )
        return candidate

    def get_result(
        self,
        run_id: str | None,
        *,
        thread_id: str | None,
    ) -> ChatResult:
        if run_id is not None:
            run = self.history.get_run_result(run_id)
        elif thread_id is None:
            raise ValueError("No run result is available in this chat.")
        else:
            try:
                run = self.history.latest_thread_result(thread_id)
            except KeyError:
                run = None
        if run is None:
            if run_id is not None:
                raise ValueError(f"Run not found: {run_id}")
            raise ValueError("No run result is available in this chat.")
        output = parts_from_local(run.output) if run.output is not None else ()
        if not output:
            raise ValueError(f"Run has no result: {run.id}")
        return ChatResult(run_id=run.id, output=output)

    def run(
        self,
        thread_id: str,
        message: str,
        selects: Mapping[str, object],
        on_event: Callable[[RunEvent], None],
        on_error: Callable[[str], None],
        on_state: Callable[[ChatRunState], None] | None = None,
    ) -> None:
        try:
            self._submit(
                self._run(thread_id, message, selects, on_event, on_state)
            ).result()
        except Exception as exc:
            on_error(_error_message(exc))

    def cancel(
        self,
        run_id: str,
        on_error: Callable[[str], None],
    ) -> None:
        self._submit_control(
            self.run_client.cancel(
                run_id,
                request_id=f"term_{uuid4().hex}",
            ),
            on_error,
        )

    def steer(
        self,
        run_id: str,
        message: str,
        on_error: Callable[[str], None],
    ) -> None:
        self._submit_control(
            self.run_client.steer(
                run_id,
                Message.user(message),
                timing="next_step",
                request_id=f"term_{uuid4().hex}",
            ),
            on_error,
        )

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
        self.executor.start()
        await self.run_client.connect()
        state = await self.state_watcher.refresh()
        setup = (
            await self.setup_watcher.refresh(progress=self._initial_progress)
            if self._initial_progress is not None
            else await self.setup_watcher.refresh()
        )
        validate_agent_ceiling(setup, state, setup.ceiling)
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
        on_state: Callable[[ChatRunState], None] | None = None,
    ) -> None:
        commands, input = parse_call(message)
        request = RunRequest(
            thread=thread_id,
            commands=commands,
            input=input,
            session_commands=commands_from_selects(selects),
            runnable_fallbacks=("agic:chat", "default"),
            request_id=f"term_{uuid4().hex}",
        )
        handle = await self.run_client.run(
            request,
            tracer=_CallbackTracer(on_event),
        )
        if on_state is not None:
            on_state(RunAccepted(handle.run_id))
        await handle.wait()

    async def _close(self) -> None:
        if self._stop_signal is not None:
            self._stop_signal.set()
        await self.run_client.disconnect()
        await self.executor.stop()
        if self._watch_tasks:
            for task in self._watch_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._watch_tasks, return_exceptions=True)

    def _submit_control(
        self,
        coroutine: Coroutine[Any, Any, Any],
        on_error: Callable[[str], None],
    ) -> None:
        try:
            future = self._submit(coroutine)
        except Exception as exc:
            on_error(_error_message(exc))
            return
        if threading.current_thread() is self._thread:
            future.add_done_callback(
                lambda completed: _finish_control(completed, on_error)
            )
            return
        _finish_control(future, on_error)

    def _submit(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        allow_closed: bool = False,
    ) -> Future[Any]:
        if self._closed and not allow_closed:
            coroutine.close()
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


def _error_message(exc: Exception) -> str:
    cause = exc.__cause__
    return str(cause or exc) or type(cause or exc).__name__


def _finish_control(
    future: Future[Any],
    on_error: Callable[[str], None],
) -> None:
    try:
        future.result()
    except Exception as exc:
        on_error(_error_message(exc))
