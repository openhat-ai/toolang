"""Direct executor-backed client for the terminal chat UI."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from toolang.agent import runtime as up
from toolang.common.error import ToolangError
from toolang.base.types.message import Message
from toolang.execution.binding import allocate_thread_id
from toolang.execution.effective import (
    effective_origin_model_selectors,
    select_origin_agic,
)
from toolang.execution.events import TraceEvent
from toolang.execution.request import ExecutableKind, RunRequest
from toolang.plugin.models.resolution import selectable_model_targets


@dataclass(slots=True)
class _TraceReply:
    on_trace: Callable[[TraceEvent], None]
    wants_stream: bool = True

    def on_event(self, event: TraceEvent) -> None:
        self.on_trace(event)


class LocalChatClient:
    """Run TUI requests directly through a process-local executor."""

    def __init__(self, root: Path, name: str, *, environ: Mapping[str, str]) -> None:
        self.executor, self.state_watcher, _ = up.assemble_execution(
            toolang_root=root,
            agent_name=name,
            enabled_components=(),
            environ=environ,
        )
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._closed = False
        self._thread.start()
        self._ready.wait()

    def list_models(self) -> Mapping[str, Any]:
        executor = self.executor
        selectors = effective_origin_model_selectors(
            executor,
            state=self.state_watcher.current(),
            origin="chat",
        )
        targets = selectable_model_targets(
            providers=executor.setup.model_providers,
            aliases=executor.model_aliases,
            environ=executor.model_environ,
            selectors=selectors,
            cache_dir=executor.model_cache_dir,
            refresh=executor.model_cache_refresh,
        )
        return {
            "default": selectors[0] if selectors else None,
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
            try:
                default = select_origin_agic(program, origin="chat").name
            except ToolangError:
                default = None
            return {
                "default": default,
                "items": [{"name": agic.name} for agic in program.available_agics],
            }
        if kind == "flow":
            return {
                "default": None,
                "items": [{"name": flow.name} for flow in program.flows],
            }
        raise ValueError(f"unknown executable kind: {kind}")

    def create_thread(self) -> str:
        thread_id = allocate_thread_id(
            self.executor.id_state_path, "tui"
        )
        self.executor.store.ensure_thread(thread_id=thread_id, origin="chat")
        return thread_id

    def start_run(
        self,
        thread_id: str,
        message: str,
        selects: Mapping[str, object],
        on_event: Callable[[TraceEvent], None],
        on_error: Callable[[str], None],
    ) -> None:
        try:
            self._submit(self._run(thread_id, message, selects, on_event)).result()
        except Exception as exc:
            on_error(_error_message(exc))

    def stop_run(
        self,
        run_id: str,
        on_event: Callable[[TraceEvent], None],
        on_error: Callable[[str], None],
    ) -> None:
        del on_event
        try:
            self._submit(
                self.executor.stop(
                    run_id=run_id,
                    request_id=f"req_{uuid4().hex}",
                )
            ).result()
        except Exception as exc:
            on_error(_error_message(exc))

    def steer_run(
        self,
        run_id: str,
        message: str,
        on_event: Callable[[TraceEvent], None],
        on_error: Callable[[str], None],
    ) -> None:
        del on_event
        try:
            self._submit(self._steer(run_id, message)).result()
        except Exception as exc:
            on_error(_error_message(exc))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._submit(self.executor.close(), allow_closed=True).result()
        self.executor.store.close()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()

    async def _run(
        self,
        thread_id: str,
        message: str,
        selects: Mapping[str, object],
        on_event: Callable[[TraceEvent], None],
    ) -> None:
        state = self.state_watcher.refresh()
        executable_kind, executable_name = _executable(selects)
        await self.executor.run(
            RunRequest(
                group="chat",
                origin="chat",
                run_id=self.executor.allocate_run_id(),
                thread_id=thread_id,
                thread_kind="tui",
                executable_kind=executable_kind,
                executable_name=executable_name,
                message=Message.user(message),
                model_selectors=_strings(selects.get("models")),
                tool_selectors=_optional_strings(selects.get("tools")),
                cap_selectors=_strings(selects.get("caps")),
                metadata={"request_id": f"term_{uuid4().hex}"},
            ),
            state,
            reply=_TraceReply(on_event),
        )

    async def _steer(self, run_id: str, message: str) -> None:
        self.executor.steer(
            run_id=run_id,
            message=Message.user(message),
            apply="next_step",
            request_id=f"req_{uuid4().hex}",
        )

    def _submit(
        self, coroutine: Any, *, allow_closed: bool = False
    ) -> Future[Any]:
        if self._closed and not allow_closed:
            raise RuntimeError("local chat client is closed")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        self._loop.close()


def _executable(
    selects: Mapping[str, object],
) -> tuple[ExecutableKind, str | None]:
    agic = _text(selects.get("agic"))
    flow = _text(selects.get("flow"))
    if agic is not None and flow is not None:
        raise ValueError("chat request cannot specify both agic and flow")
    return ("flow", flow) if flow is not None else ("agic", agic)


def _optional_strings(value: object) -> tuple[str, ...] | None:
    return None if value is None else _strings(value)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _error_message(exc: Exception) -> str:
    cause = exc.__cause__
    return str(cause or exc) or type(cause or exc).__name__
