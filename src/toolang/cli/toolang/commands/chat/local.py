"""Process-local execution for terminal chat sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from toolang.base.types.message import Message
from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.execution.calls import IncludeResolver, parse_call, validate_commands
from toolang.execution.client import LocalRunClient, RunClient
from toolang.execution.events import RunEvent, RunTracer
from toolang.execution.executor import RunExecutor
from toolang.execution.runnables import runnable_binding_defaults
from toolang.execution.executor.resources import (
    agent_model_targets,
    validate_agent_ceiling,
)
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.schemas import RunRequest
from toolang.execution.types import RunOverride, ThreadPrefix
from toolang.lang.includes import resolve_file_include
from toolang.setup import AgentSetup, SetupWatcher
from toolang.state.state import AgentState
from toolang.state.watcher import StateWatcher
from .base import ChatResult
from .policy import apply_session_commands, commands_from_selects


@dataclass(slots=True)
class _CallbackTracer(RunTracer):
    callback: Callable[[RunEvent], None]

    async def on_event(self, event: RunEvent) -> None:
        self.callback(event)


class LocalChatSession:
    """Expose the chat-client contract over one process-local executor."""

    executor_label = "embedded"

    def __init__(
        self,
        layout: AgentLayout,
        *,
        model_catalog: Path | None = None,
        ceiling_overrides: Mapping[str, tuple[str, ...] | None] | None = None,
        binding_overrides: Mapping[str, str | None] | None = None,
        limit_overrides: Mapping[str, int | Decimal | None] | None = None,
    ) -> None:
        self.layout = layout
        self.store = RunStore(layout.run_store)
        self.ids = IdIssuer(layout.id_state)
        self.threads = ThreadManager(self.store, self.ids)
        self.setup_watcher = SetupWatcher(
            layout,
            model_catalog=model_catalog,
            ceiling_overrides=ceiling_overrides,
            binding_overrides=binding_overrides,
            limit_overrides=limit_overrides,
        )
        self.state_watcher = StateWatcher(layout)
        self.run_client: RunClient = LocalRunClient(
            RunExecutor(self.store, self.ids),
            setup=self.setup_watcher.current,
            state=self.state_watcher.current,
            include=self._include_resolver,
        )
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

    def list_executables(self, kind: str) -> Mapping[str, Any]:
        setup = self.setup_watcher.current()
        program = self.state_watcher.current().program
        default_agic, default_flow = runnable_binding_defaults(
            program,
            setup.bindings.runnable,
            fallback_agic="chat",
        )
        if kind == "agic":
            names = [agic.name for agic in program.agics]
            default = default_agic
            if "default" not in names:
                names.append("default")
            return {
                "default": default,
                "items": [{"name": name} for name in names],
            }
        if kind == "flow":
            return {
                "default": default_flow,
                "items": [{"name": flow.name} for flow in program.flows],
            }
        if kind == "runnable":
            return {
                "default": setup.bindings.runnable or f"agic:{default_agic}",
                "items": [{"kind": "agic", "name": agic.name} for agic in program.agics]
                + [{"kind": "flow", "name": flow.name} for flow in program.flows],
            }
        raise ValueError(f"unknown executable kind: {kind}")

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
        validate_commands(
            commands_from_selects(candidate),
            setup=setup,
            state=state,
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
                    and candidate.status == "succeeded"
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
            self._submit(
                self.run_client.stop(
                    run_id,
                    request_id=f"term_{uuid4().hex}",
                )
            ).result()
        except Exception as exc:
            on_error(_error_message(exc))

    def steer_run(
        self,
        run_id: str,
        message: str,
        on_error: Callable[[str], None],
    ) -> None:
        try:
            self._submit(
                self.run_client.steer(
                    run_id,
                    Message.user(message),
                    timing="next_step",
                    request_id=f"term_{uuid4().hex}",
                )
            ).result()
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
    ) -> None:
        commands, input = parse_call(message)
        request = RunRequest(
            thread=thread_id,
            commands=commands,
            input=input,
            session_commands=commands_from_selects(selects),
            runnable_fallbacks=("chat", "default"),
            request_id=f"term_{uuid4().hex}",
        )
        handle = await self.run_client.start(
            request,
            tracer=_CallbackTracer(on_event),
        )
        await handle.wait()

    async def _close(self) -> None:
        if self._stop_signal is not None:
            self._stop_signal.set()
        await self.run_client.close()
        if self._watch_tasks:
            for task in self._watch_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._watch_tasks, return_exceptions=True)

    def _include_resolver(self, setup: AgentSetup) -> IncludeResolver:
        base = (
            setup.environment.working_directory
            if setup.environment is not None
            else self.layout.home
        )
        return lambda reference: resolve_file_include(reference, base=base)

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


def _error_message(exc: Exception) -> str:
    cause = exc.__cause__
    return str(cause or exc) or type(cause or exc).__name__
