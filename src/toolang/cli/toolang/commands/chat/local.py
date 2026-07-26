"""Process-local execution for terminal chat sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, replace
import threading
from typing import Any
from uuid import uuid4

from toolang.base.types.message import Message
from toolang.common.errors import ToolangError
from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.execution.events import RunEvent, RunTracer
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.types import ThreadPrefix
from toolang.lang.input import perceive_input
from toolang.plugin.models.config import parse_default_models, parse_model_aliases
from toolang.plugin.models.resolution import selectable_model_targets
from toolang.plugin.tools.loading import select_tools, validate_tool_selectors
from toolang.setup import SetupWatcher
from toolang.state import state as cap_state
from toolang.state.state import AgentState
from toolang.state.watcher import StateWatcher


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
        self.model_selectors = tuple(models)
        self.tool_selectors = tuple(tools) if tools is not None else None
        self.cap_selectors = tuple(caps)
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
        layers = (state.root_config, state.home_config)
        aliases = parse_model_aliases(layers)
        defaults = self.model_selectors or parse_default_models(layers)
        targets = selectable_model_targets(
            providers=setup.providers,
            models=setup.models,
            aliases=aliases,
            envs=setup.envs,
            selectors=self.model_selectors or None,
        )
        default = defaults[0] if defaults else targets[0][0] if targets else None
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
        await self.state_watcher.refresh()
        setup = await self.setup_watcher.refresh()
        if self.tool_selectors is not None:
            validate_tool_selectors(dict(setup.tools), self.tool_selectors)
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
        tool_selectors = _strings(selects.get("tools"))
        if "tools" in selects:
            validate_tool_selectors(dict(setup.tools), tool_selectors)
            setup = replace(
                setup,
                tools=select_tools(dict(setup.tools), tool_selectors),
            )
        elif self.tool_selectors is not None:
            setup = replace(
                setup,
                tools=select_tools(dict(setup.tools), self.tool_selectors),
            )
        cap_selectors = _strings(selects.get("caps"))
        if cap_selectors:
            state = self._state_with_caps(state, cap_selectors)
        elif self.cap_selectors:
            state = self._state_with_caps(state, self.cap_selectors)
        runnable = _runnable(state, selects)
        model = next(iter(_strings(selects.get("models"))), None)
        handle = self.executor.start(
            RunSpec(
                setup=setup,
                state=state,
                thread=thread_id,
                runnable=runnable,
                input=perceive_input(message, program=state.program),
                model=model,
            ),
            request_id=f"term_{uuid4().hex}",
            tracer=_CallbackTracer(on_event),
        )
        await handle

    def _state_with_caps(
        self,
        state: AgentState,
        selectors: Sequence[str],
    ) -> AgentState:
        missing = [
            selector
            for selector in selectors
            if not cap_state.select_cap_entries(
                state.caps,
                (selector,),
                agent_name=self.layout.name,
            )
        ]
        if missing:
            raise ToolangError(f"cap selector matched no caps: {', '.join(missing)}")
        selected = cap_state.select_cap_entries(
            state.caps,
            tuple(selectors),
            agent_name=self.layout.name,
        )
        return replace(state, caps=selected)

    async def _close(self) -> None:
        if self._stop_signal is not None:
            self._stop_signal.set()
        await self.executor.shutdown()
        if self._watch_tasks:
            await asyncio.gather(*self._watch_tasks, return_exceptions=True)

    def _submit(self, coroutine: Any, *, allow_closed: bool = False) -> Future[Any]:
        if self._closed and not allow_closed:
            raise RuntimeError("local chat session is closed")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._stop_signal = asyncio.Event()
        self._ready.set()
        self._loop.run_forever()
        self._loop.close()


def _runnable(state: AgentState, selects: Mapping[str, object]) -> str:
    agic = _text(selects.get("agic"))
    flow = _text(selects.get("flow"))
    if agic is not None and flow is not None:
        raise ValueError("chat request cannot specify both agic and flow")
    if flow is not None:
        return flow
    if agic is not None:
        return agic
    return "chat" if state.program.find_agic("chat") is not None else "default"


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _error_message(exc: Exception) -> str:
    cause = exc.__cause__
    return str(cause or exc) or type(cause or exc).__name__
