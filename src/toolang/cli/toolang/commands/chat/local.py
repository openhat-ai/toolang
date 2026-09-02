"""Process-local execution for terminal chat sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import threading
from typing import Any, cast
from uuid import uuid4

from toolang.base.types.message import Message
from toolang.base.types.model import ModelOverride, ModelRequest
from toolang.base.types.policy import AgentCeiling
from toolang.common.ids import IdIssuer
from toolang.common.layout import AgentLayout
from toolang.execution.calls import materialize_model_request
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
    snapshot_model_selection,
    validate_agent_ceiling,
)
from toolang.plugin.models.resolution import (
    model_reasoning_effort_applicable,
    model_reasoning_efforts,
)
from toolang.plugin.toolsets.collections import tool_dataset
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.schemas import RunRequest
from toolang.execution.types import RunOverride, SessionSetting, ThreadPrefix
from toolang.lang.input import RunnableInputRaw
from toolang.plugin.sandboxes.host import host_sandbox_description
from toolang.setup import AgentSetup, SetupWatcher
from toolang.state.watcher import StateWatcher
from toolang.state.collections import cap_dataset, query_cap_views
from toolang.state.schemas import CapInfo
from toolang.state.types import EntryKind
from toolang.state.state import (
    AgentState,
    StateCap,
    StatePublication,
    state_module_caps,
    state_program,
)
from toolang.execution.values import parts_from_local
from .base import ChatExecutorMetadata, ChatResult, ChatRunState, RunAccepted
from .policy import (
    build_run_request,
    reconcile_session_model,
    session_model_reconciliation_required,
    update_session_setting,
)


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
        default_overrides: Mapping[str, ModelOverride | str | None] | None = None,
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
        self._surface: SessionSetting | None = None
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

    def list_models(
        self,
        queries: Sequence[str] | None = None,
    ) -> Mapping[str, Any]:
        setup = self.setup_watcher.current()
        if queries is not None and not queries:
            return {"default": None, "items": []}
        models = setup.models.match(queries)
        selection = snapshot_model_selection(setup)
        preferred = (
            setup.defaults.model.ref if setup.defaults.model is not None else None
        )
        return {
            "default": models.effective_default(preferred),
            "items": [
                {
                    "ref": ref,
                    "name": target.name,
                    "provider": target.provider,
                    "parameters": {
                        "reasoning": {
                            "effort": list(model_reasoning_efforts(selection, target)),
                            "applicable": model_reasoning_effort_applicable(
                                selection, target
                            ),
                        }
                    },
                    "price": {
                        "input": _price_per_million(entry.info.input_price),
                        "output": _price_per_million(entry.info.output_price),
                    },
                }
                for entry in models.entries
                for ref, target in ((entry.ref, entry.target),)
            ],
        }

    def list_tools(
        self,
        queries: Sequence[str] | None = None,
    ) -> Mapping[str, Any]:
        if queries is not None and not queries:
            return {"items": []}
        tools = self.setup_watcher.current().tools
        selected = tool_dataset(tools).query(queries)
        return {
            "items": [
                {
                    "ref": f"{item.toolset}/{item.name}",
                    "toolset": item.toolset,
                    "plugin": item.plugin,
                    "description": item.description,
                }
                for item in selected
            ]
        }

    def list_caps(
        self,
        kind: str | None = None,
        queries: Sequence[str] | None = None,
    ) -> Mapping[str, Any]:
        state = self.state_watcher.current()
        entries = state_module_caps(state, "agent")
        if kind is None:
            matched = (
                query_cap_views(
                    entries,
                    agent_name=self.layout.name,
                    queries=queries,
                )
                if queries is None or queries
                else ()
            )
            selected = tuple(
                item
                for item_kind in ("psyche", "skill", "service", "prompt")
                for item in matched
                if item.kind == item_kind
            )
        elif kind in {"psyche", "skill", "service", "prompt"}:
            dataset = cap_dataset(
                entries,
                agent_name=self.layout.name,
                kind=cast(EntryKind, kind),
            )
            selected = (
                dataset.query(None)
                if queries is None
                else dataset.query(queries)
                if queries
                else ()
            )
        else:
            raise ValueError(f"unknown cap kind: {kind}")
        return {
            "items": [
                _local_cap_item(
                    cast(StateCap, item.record), agent_name=self.layout.name
                )
                for item in selected
            ]
        }

    def list_runnables(self, kind: str) -> Mapping[str, Any]:
        state = self.state_watcher.current()
        default_ref = self.initial_setting().runnable
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
        state = self.state_watcher.current()
        selected = runnable or self.initial_setting().runnable
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

    def initial_setting(self) -> SessionSetting:
        if self._surface is None:
            raise RuntimeError("local chat session settings are not initialized")
        return self._surface

    def apply_setting(
        self,
        setting: SessionSetting,
        update: RunOverride,
        *,
        allowed_model_refs: Sequence[str] | None = None,
        default_model_ref: str | None = None,
    ) -> SessionSetting:
        candidate = update_session_setting(
            surface=self.initial_setting(),
            current=setting,
            update=update,
        )
        if not session_model_reconciliation_required(update):
            return candidate
        if allowed_model_refs is None:
            payload = self.list_models(candidate.allow.models)
            allowed_model_refs = tuple(
                str(item["ref"])
                for item in payload["items"]
                if isinstance(item, Mapping) and isinstance(item.get("ref"), str)
            )
            default_model_ref = (
                str(payload["default"])
                if isinstance(payload.get("default"), str)
                else None
            )
        return reconcile_session_model(
            candidate,
            update,
            allowed_refs=allowed_model_refs,
            default_ref=default_model_ref,
        )

    def build_request(
        self,
        thread_id: str,
        override: RunOverride,
        input: RunnableInputRaw,
        setting: SessionSetting,
    ) -> RunRequest:
        return build_run_request(
            thread_id=thread_id,
            request_id=f"term_{uuid4().hex}",
            input=input,
            override=override,
            setting=setting,
            surface=self.initial_setting(),
            resolve_model_ref=self._materialize_model_ref,
            resolve_runnable_ref=self._materialize_runnable_ref,
        )

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
        request: RunRequest,
        on_event: Callable[[RunEvent], None],
        on_error: Callable[[str], None],
        on_state: Callable[[ChatRunState], None] | None = None,
    ) -> None:
        try:
            self._submit(self._run(request, on_event, on_state)).result()
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
        state, setup = await asyncio.gather(
            self.state_watcher.refresh(),
            self.setup_watcher.refresh(),
        )
        validate_agent_ceiling(setup, state, AgentCeiling())
        self._surface = self._current_session_setting(setup=setup, state=state)
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
        request: RunRequest,
        on_event: Callable[[RunEvent], None],
        on_state: Callable[[ChatRunState], None] | None = None,
    ) -> None:
        handle = await self.run_client.run(
            request,
            tracer=_CallbackTracer(on_event),
        )
        if on_state is not None:
            on_state(RunAccepted(handle.run_id))
        await handle.wait()

    def _materialize_model_ref(self, ref: str) -> str:
        return materialize_model_request(
            ModelRequest(ref),
            setup=self.setup_watcher.current(),
        ).ref

    def _materialize_runnable_ref(self, query: str) -> str:
        state = self.state_watcher.current()
        return resolve_public_runnable_query(state, query).ref

    @staticmethod
    def _current_session_setting(
        *, setup: AgentSetup, state: AgentState | StatePublication
    ) -> SessionSetting:
        model = setup.defaults.model
        if model is None:
            fallback = setup.models.effective_default(None)
            model = ModelRequest(fallback) if fallback is not None else None
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
        return SessionSetting(
            model=model,
            runnable=runnable,
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


def _price_per_million(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value)) * Decimal(1_000_000)


def _local_cap_item(cap: StateCap, *, agent_name: str) -> dict[str, object]:
    info = CapInfo.from_cap(cap, agent_name=agent_name)
    return {
        "identity": f"{info.kind}/{info.name}",
        "kind": info.kind,
        "scope": info.scope,
        "form": info.form,
        "description": info.description or "",
        "summary": info.summary or "",
    }


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
