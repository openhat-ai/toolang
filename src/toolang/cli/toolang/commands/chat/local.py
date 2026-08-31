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
from toolang.base.types.model import ModelRequest
from toolang.base.types.policy import AgentCeiling, RunBindings
from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.execution.calls import materialize_model_request, parse_call
from toolang.execution.client import LocalRunClient, RunClient
from toolang.execution.events import RunEvent, RunTracer
from toolang.execution.executor import RunExecutor
from toolang.execution.history import RunHistory
from toolang.execution.runnables import (
    parse_runnable_ref,
    runnable_binding_defaults,
    resolve_public_runnable_query,
)
from toolang.execution.executor.resources import (
    agent_model_targets,
    snapshot_model_selection,
    validate_agent_ceiling,
)
from toolang.plugin.models.resolution import model_reasoning_efforts
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.types import RunOverride, ThreadPrefix
from toolang.plugin.sandboxes.host import host_sandbox_description
from toolang.setup import AgentSetup, SetupWatcher
from toolang.state.watcher import StateWatcher
from toolang.state.state import AgentState, StatePublication, state_program
from toolang.execution.values import parts_from_local
from .base import ChatExecutorMetadata, ChatResult, ChatRunState, RunAccepted
from .policy import ChatRunDefaults, apply_session_commands, build_run_request


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
        default_overrides: Mapping[str, str | None] | None = None,
        limit_overrides: Mapping[str, int | Decimal | None] | None = None,
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
        allow_overrides = dict(ceiling_overrides or {})
        self.setup_watcher = SetupWatcher(
            layout,
            sandbox=sandbox,
            model_catalog=model_catalog,
            allow_overrides={
                name: value
                for name, value in allow_overrides.items()
                if name in {"models", "tools"}
            },
            default_overrides=default_overrides,
            limit_overrides=limit_overrides,
        )
        self.state_watcher = StateWatcher(
            layout,
            allow_overrides={
                name: value
                for name, value in allow_overrides.items()
                if name in {"psyches", "skills", "services", "prompts"}
            },
        )
        self.executor = RunExecutor(
            self.store,
            self.ids,
            setup=self.setup_watcher.current,
            state=self.state_watcher.current,
            load_state=lambda revision: self.state_watcher.load(revision),
            refresh_state=self.state_watcher.refresh_result,
        )
        self.run_client: RunClient = LocalRunClient(self.executor)
        self._defaults: ChatRunDefaults | None = None
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
        default, targets = agent_model_targets(setup, AgentCeiling())
        selection = snapshot_model_selection(setup)
        return {
            "default": default,
            "items": [
                {
                    "ref": ref,
                    "name": target.name,
                    "provider": target.provider,
                    "parameters": {
                        "reasoning": {
                            "effort": list(model_reasoning_efforts(selection, target))
                        }
                    },
                }
                for ref, target in targets
            ],
        }

    def list_runnables(self, kind: str) -> Mapping[str, Any]:
        state = self._submit(self.state_watcher.refresh()).result()
        default_ref = self._run_defaults().bindings.runnable
        default_name, default_kind = (
            parse_runnable_ref(default_ref) if default_ref is not None else (None, None)
        )
        if kind == "agic":
            names = list(state.agics)
            return {
                "default": default_name if default_kind == "agic" else None,
                "items": [{"name": name} for name in names],
            }
        if kind == "flow":
            return {
                "default": default_name if default_kind == "flow" else None,
                "items": [{"name": name} for name in state.flows],
            }
        if kind == "runnable":
            return {
                "default": default_ref,
                "items": [
                    {"kind": item.kind, "name": name}
                    for name, item in state.runnables.items()
                ],
            }
        raise ValueError(f"unknown runnable kind: {kind}")

    def list_prompts(self, runnable: str | None) -> Mapping[str, Any]:
        state = self._submit(self.state_watcher.refresh()).result()
        selected = runnable or self._run_defaults().bindings.runnable
        if selected is None:  # pragma: no cover - initialization invariant
            raise RuntimeError("chat has no default runnable")
        module = resolve_public_runnable_query(state, selected).module
        return {
            "items": [
                {
                    "name": prompt.name,
                    "params": [
                        {"name": parameter.name, "optional": parameter.optional}
                        for parameter in prompt.params
                    ],
                }
                for prompt in state_program(state, module).caps
                if prompt.kind == "prompt"
            ]
        }

    def create_thread(self) -> str:
        return self.threads.create(prefix=ThreadPrefix.TERM)

    def apply_settings(
        self,
        commands: tuple[RunOverride, ...],
        selects: Mapping[str, object],
    ) -> Mapping[str, object]:
        return apply_session_commands(selects, commands)

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
        setup = await self.setup_watcher.refresh()
        validate_agent_ceiling(setup, state, AgentCeiling())
        self._defaults = self._current_run_defaults(setup=setup, state=state)
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
        request = build_run_request(
            thread_id=thread_id,
            request_id=f"term_{uuid4().hex}",
            input=input,
            input_commands=commands,
            selects=selects,
            defaults=self._run_defaults(),
            resolve_model_ref=self._materialize_model_ref,
            resolve_runnable_ref=self._materialize_runnable_ref,
        )
        handle = await self.run_client.run(
            request,
            tracer=_CallbackTracer(on_event),
        )
        if on_state is not None:
            on_state(RunAccepted(handle.run_id))
        await handle.wait()

    def _run_defaults(self) -> ChatRunDefaults:
        if self._defaults is None:
            raise RuntimeError("local chat run defaults are not initialized")
        return self._defaults

    def _materialize_model_ref(self, ref: str) -> str:
        return materialize_model_request(
            ModelRequest(ref),
            setup=self.setup_watcher.current(),
        ).ref

    def _materialize_runnable_ref(self, query: str) -> str:
        state = self.state_watcher.current()
        return resolve_public_runnable_query(state, query).ref

    @staticmethod
    def _current_run_defaults(
        *, setup: AgentSetup, state: AgentState | StatePublication
    ) -> ChatRunDefaults:
        model, _targets = agent_model_targets(setup, AgentCeiling())
        runnable = setup.defaults.runnable
        if runnable is None:
            default_agic, default_flow = runnable_binding_defaults(
                state,
                None,
                fallback_agic="chat",
            )
            if default_agic is not None:
                runnable = f"agic:{default_agic}"
            elif default_flow is not None:
                runnable = f"flow:{default_flow}"
        if runnable is not None:
            runnable = resolve_public_runnable_query(state, runnable).ref
        return ChatRunDefaults(
            bindings=RunBindings(model=model, runnable=runnable),
            limits=setup.limits,
        )

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
