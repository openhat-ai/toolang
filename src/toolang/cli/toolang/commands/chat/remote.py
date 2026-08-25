"""Resident AgentServer execution for terminal chat sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from decimal import Decimal
import json
import re
import threading
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import TypeAdapter, ValidationError

from toolang.base.types.message import Message
from toolang.common.errors import ToolangError
from toolang.execution.calls import parse_call
from toolang.execution.events import RunEvent, RunTracer
from toolang.execution.remote import RemoteRunClient, RemoteRunClientError
from toolang.execution.schemas import RunDetail, RunRequest, ThreadInfo
from toolang.execution.types import RunOverride
from toolang.execution.values import parts_from_local

from .base import (
    ChatExecutorMetadata,
    ChatResult,
    ChatRunState,
    RunAccepted,
    RunBlocked,
    RunDisconnected,
    RunRecovered,
)
from .policy import apply_session_commands, commands_from_selects


_RUN_DETAIL_ADAPTER = TypeAdapter(RunDetail)
_THREAD_INFO_ADAPTER = TypeAdapter(ThreadInfo)
_RECOVERY_DELAYS = (0.5, 1.0, 2.0)
_RECOVERY_INTERVAL = 5.0


class RemoteChatError(ToolangError):
    """One sanitized remote Chat HTTP or protocol failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class _RemoteChatProtocolError(RemoteChatError):
    pass


@dataclass(slots=True)
class _CallbackTracer(RunTracer):
    callback: Callable[[RunEvent], None]

    async def on_event(self, event: RunEvent) -> None:
        self.callback(event)


@dataclass(frozen=True, slots=True)
class _RuntimeIdentity:
    version: str
    driver: str
    selector: str
    instance: str | None


class RemoteChatSession:
    """Expose the Chat client contract through one resident AgentServer."""

    def __init__(
        self,
        endpoint: str,
        *,
        expected_sandbox: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._expected_sandbox = expected_sandbox.strip()
        if not self._expected_sandbox or self._expected_sandbox != expected_sandbox:
            raise ValueError("remote chat requires the running sandbox identity")
        self._transport = transport
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._close_signal: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._http: httpx.AsyncClient | None = None
        self.run_client: RemoteRunClient | None = None
        self.executor_metadata: ChatExecutorMetadata
        self._blocked_run_id: str | None = None
        self._blocked_message: str | None = None
        self._closed = False
        self._thread.start()
        self._ready.wait()
        try:
            self._submit(self._initialize()).result()
        except Exception:
            self.close()
            raise

    def list_models(self) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], self._submit(self._list_models()).result())

    def list_executables(self, kind: str) -> Mapping[str, Any]:
        return cast(
            Mapping[str, Any],
            self._submit(self._list_executables(kind)).result(),
        )

    def create_thread(self) -> str:
        return cast(str, self._submit(self._create_thread()).result())

    def apply_settings(
        self,
        commands: tuple[RunOverride, ...],
        selects: Mapping[str, object],
    ) -> Mapping[str, object]:
        return cast(
            Mapping[str, object],
            self._submit(self._apply_settings(commands, selects)).result(),
        )

    def get_result(
        self,
        run_id: str | None,
        *,
        thread_id: str | None,
    ) -> ChatResult:
        return cast(
            ChatResult,
            self._submit(self._get_result(run_id, thread_id=thread_id)).result(),
        )

    def start_run(
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
                self._run(
                    thread_id,
                    message,
                    selects,
                    on_event,
                    on_state,
                )
            ).result()
        except Exception as exc:
            on_error(_error_message(exc))

    def stop_run(
        self,
        run_id: str,
        on_error: Callable[[str], None],
    ) -> None:
        run_client = self._run_client()
        self._submit_control(
            run_client.stop(
                run_id,
                request_id=f"term_{uuid4().hex}",
            ),
            on_error,
        )

    def steer_run(
        self,
        run_id: str,
        message: str,
        on_error: Callable[[str], None],
    ) -> None:
        run_client = self._run_client()
        self._submit_control(
            run_client.steer(
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

    async def _initialize(self) -> None:
        self._http = httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(3.0),
        )
        self.run_client = RemoteRunClient(self._endpoint, client=self._http)
        health = await self._request_json("GET", "/healthz", operation="health")
        if health != {"ok": True}:
            raise _RemoteChatProtocolError(
                "remote chat health check returned invalid data"
            )
        profile = await self._request_json(
            "GET",
            "/api/v1/profile",
            operation="profile",
        )
        identity = _runtime_identity(profile)
        if identity.selector != self._expected_sandbox:
            raise RemoteChatError(
                "running executor sandbox does not match its runtime status"
            )
        port = urlsplit(self.run_client.endpoint).port
        if port is None:
            raise RemoteChatError("running executor endpoint has no explicit port")
        self.executor_metadata = ChatExecutorMetadata(
            endpoint=self.run_client.endpoint,
            version=identity.version,
            sandbox=identity.selector if identity.driver != "host" else None,
            instance=identity.instance,
        )

    async def _list_models(self) -> dict[str, object]:
        payload = await self._request_json(
            "GET",
            "/api/v1/models",
            operation="models",
        )
        return _catalog_payload(payload, operation="models", item_kind="model")

    async def _list_executables(self, kind: str) -> dict[str, object]:
        if kind in {"agic", "flow"}:
            payload = await self._request_json(
                "GET",
                f"/api/v1/{kind}s",
                operation=kind,
            )
            return _catalog_payload(
                payload,
                operation=kind,
                item_kind="executable",
            )
        if kind != "runnable":
            raise ValueError(f"unknown executable kind: {kind}")
        agics, flows = await asyncio.gather(
            self._request_json("GET", "/api/v1/agics", operation="agics"),
            self._request_json("GET", "/api/v1/flows", operation="flows"),
        )
        agic_payload = _catalog_payload(
            agics,
            operation="agics",
            item_kind="executable",
        )
        flow_payload = _catalog_payload(
            flows,
            operation="flows",
            item_kind="executable",
        )
        defaults = [
            f"{candidate_kind}:{default}"
            for candidate_kind, default in (
                ("agic", agic_payload["default"]),
                ("flow", flow_payload["default"]),
            )
            if isinstance(default, str)
        ]
        if len(defaults) != 1:
            raise _RemoteChatProtocolError(
                "remote chat runnables returned invalid defaults"
            )
        return {
            "default": defaults[0],
            "items": [
                *(
                    {"kind": "agic", **item}
                    for item in cast(list[dict[str, object]], agic_payload["items"])
                ),
                *(
                    {"kind": "flow", **item}
                    for item in cast(list[dict[str, object]], flow_payload["items"])
                ),
            ],
        }

    async def _create_thread(self) -> str:
        payload = await self._request_json(
            "POST",
            "/api/v1/threads",
            operation="create thread",
            json={"client": "tui"},
        )
        body = _mapping(payload, operation="create thread")
        if set(body) != {"thread"}:
            raise _RemoteChatProtocolError(
                "remote chat create thread returned invalid data"
            )
        try:
            thread = _THREAD_INFO_ADAPTER.validate_python(body["thread"])
        except ValidationError as exc:
            raise _RemoteChatProtocolError(
                "remote chat create thread returned invalid data"
            ) from exc
        return thread.id

    async def _apply_settings(
        self,
        commands: tuple[RunOverride, ...],
        selects: Mapping[str, object],
    ) -> dict[str, object]:
        if self._blocked_message is not None:
            raise RemoteChatError(self._blocked_message)
        candidate = apply_session_commands(selects, commands)
        await self._request(
            "POST",
            "/api/v1/runs/authored/validate",
            operation="validate settings",
            json={
                "session_commands": [
                    _run_override_data(item)
                    for item in commands_from_selects(candidate)
                ],
                "runnable_fallbacks": ["agic:chat", "default"],
            },
        )
        return candidate

    async def _get_result(
        self,
        run_id: str | None,
        *,
        thread_id: str | None,
    ) -> ChatResult:
        if run_id is not None:
            path = f"/api/v1/runs/{run_id}"
            operation = "get run result"
        elif thread_id is not None:
            path = f"/api/v1/threads/{thread_id}/result"
            operation = "get latest result"
        else:
            raise ValueError("No run result is available in this chat.")
        try:
            detail = await self._run_detail(path, operation=operation)
        except RemoteChatError as exc:
            if exc.status_code == 404:
                if run_id is not None:
                    raise ValueError(f"Run not found: {run_id}") from None
                raise ValueError("No run result is available in this chat.") from None
            raise
        if run_id is not None and detail.id != run_id:
            raise _RemoteChatProtocolError(
                "remote chat run result returned mismatched identity"
            )
        output = parts_from_local(detail.output) if detail.output is not None else ()
        if not output:
            raise ValueError(f"Run has no result: {detail.id}")
        return ChatResult(run_id=detail.id, output=output)

    async def _run(
        self,
        thread_id: str,
        message: str,
        selects: Mapping[str, object],
        on_event: Callable[[RunEvent], None],
        on_state: Callable[[ChatRunState], None] | None,
    ) -> None:
        if self._blocked_message is not None:
            _emit_state(
                on_state,
                RunBlocked(self._blocked_run_id, self._blocked_message),
            )
            return
        commands, input_value = parse_call(message)
        request = RunRequest(
            thread=thread_id,
            commands=commands,
            input=input_value,
            session_commands=commands_from_selects(selects),
            runnable_fallbacks=("agic:chat", "default"),
            request_id=f"term_{uuid4().hex}",
        )
        run_client = self._run_client()
        try:
            handle = await run_client.start(
                request,
                tracer=_CallbackTracer(on_event),
            )
        except RemoteRunClientError as exc:
            if exc.status_code is not None:
                raise
            self._block(
                None,
                "Run acceptance could not be confirmed. Restart Chat before "
                "submitting again.",
                on_state,
            )
            return

        _emit_state(on_state, RunAccepted(handle.run_id))
        try:
            await handle.wait()
            return
        except RemoteRunClientError:
            if self._closed:
                return
        message_text = (
            "Connection to the executor was lost; waiting for durable run state."
        )
        _emit_state(on_state, RunDisconnected(handle.run_id, message_text))
        await self._recover(handle.run_id, on_state)

    async def _recover(
        self,
        run_id: str,
        on_state: Callable[[ChatRunState], None] | None,
    ) -> None:
        attempt = 0
        while not self._closed:
            delay = (
                _RECOVERY_DELAYS[attempt]
                if attempt < len(_RECOVERY_DELAYS)
                else _RECOVERY_INTERVAL
            )
            attempt += 1
            if await self._recovery_closed(delay):
                return
            try:
                detail = await self._run_detail(
                    f"/api/v1/runs/{run_id}",
                    operation="recover run",
                )
            except _RemoteChatProtocolError:
                self._block(
                    run_id,
                    "Accepted run state is invalid. Restart Chat before submitting "
                    "again.",
                    on_state,
                )
                return
            except RemoteChatError as exc:
                if exc.status_code == 404:
                    self._block(
                        run_id,
                        "Accepted run is missing from durable history. Restart Chat "
                        "before submitting again.",
                        on_state,
                    )
                    return
                continue
            if (
                detail.id != run_id
                or detail.root_run_id != run_id
                or detail.parent is not None
            ):
                self._block(
                    run_id,
                    "Accepted run identity changed during recovery. Restart Chat "
                    "before submitting again.",
                    on_state,
                )
                return
            if detail.status in {"pending", "running"}:
                continue
            _emit_state(on_state, RunRecovered(detail))
            return

    async def _recovery_closed(self, delay: float) -> bool:
        signal = self._close_signal
        if signal is None:
            return self._closed
        try:
            await asyncio.wait_for(signal.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True

    def _block(
        self,
        run_id: str | None,
        message: str,
        on_state: Callable[[ChatRunState], None] | None,
    ) -> None:
        if self._blocked_message is None:
            self._blocked_run_id = run_id
            self._blocked_message = message
        _emit_state(
            on_state,
            RunBlocked(self._blocked_run_id, self._blocked_message),
        )

    async def _run_detail(self, path: str, *, operation: str) -> RunDetail:
        payload = await self._request_json("GET", path, operation=operation)
        try:
            return _RUN_DETAIL_ADAPTER.validate_python(payload)
        except ValidationError as exc:
            raise _RemoteChatProtocolError(
                f"remote chat {operation} returned invalid run detail"
            ) from exc

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        **kwargs: Any,
    ) -> object:
        response = await self._request(
            method,
            path,
            operation=operation,
            **kwargs,
        )
        try:
            return response.json()
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _RemoteChatProtocolError(
                f"remote chat {operation} returned invalid JSON"
            ) from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        **kwargs: Any,
    ) -> httpx.Response:
        http = self._http_client()
        try:
            response = await http.request(
                method,
                f"{self._run_client().endpoint}{path}",
                **kwargs,
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            raise RemoteChatError(
                f"remote chat {operation} transport failed: {type(exc).__name__}"
            ) from exc
        if not response.is_success:
            raise _http_error(response, operation=operation)
        return response

    async def _close(self) -> None:
        if self._close_signal is not None:
            self._close_signal.set()
        if self.run_client is not None:
            await self.run_client.close()
        if self._http is not None:
            await self._http.aclose()

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
            raise RuntimeError("remote chat session is closed")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._close_signal = asyncio.Event()
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            _close_event_loop(self._loop)
            asyncio.set_event_loop(None)

    def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("remote chat HTTP client is not initialized")
        return self._http

    def _run_client(self) -> RemoteRunClient:
        if self.run_client is None:
            raise RuntimeError("remote run client is not initialized")
        return self.run_client


def _runtime_identity(payload: object) -> _RuntimeIdentity:
    profile = _mapping(payload, operation="profile")
    runtime = _mapping(profile.get("runtime"), operation="profile runtime")
    if set(runtime) != {"version", "sandbox"}:
        raise _RemoteChatProtocolError(
            "remote chat profile returned invalid runtime identity"
        )
    sandbox = _mapping(runtime.get("sandbox"), operation="profile sandbox")
    if set(sandbox) != {"driver", "selector", "instance"}:
        raise _RemoteChatProtocolError(
            "remote chat profile returned invalid sandbox identity"
        )
    version = _label(runtime.get("version"), label="runtime version")
    driver = _token(sandbox.get("driver"), label="sandbox driver")
    selector = _label(sandbox.get("selector"), label="sandbox selector")
    if selector.partition(":")[0] != driver:
        raise _RemoteChatProtocolError(
            "remote chat sandbox selector does not match its driver"
        )
    instance_value = sandbox.get("instance")
    if driver == "host":
        if instance_value is not None:
            raise _RemoteChatProtocolError(
                "remote chat host sandbox returned an instance ID"
            )
        instance = None
    else:
        instance = _token(instance_value, label="sandbox instance")
        if len(instance) != 12:
            raise _RemoteChatProtocolError(
                "remote chat sandbox instance must contain twelve characters"
            )
    return _RuntimeIdentity(version, driver, selector, instance)


def _catalog_payload(
    payload: object,
    *,
    operation: str,
    item_kind: str,
) -> dict[str, object]:
    body = _mapping(payload, operation=operation)
    if set(body) != {"default", "items"}:
        raise _RemoteChatProtocolError(f"remote chat {operation} returned invalid data")
    default = body.get("default")
    if default is not None and not isinstance(default, str):
        raise _RemoteChatProtocolError(
            f"remote chat {operation} returned invalid default"
        )
    raw_items = body.get("items")
    if not isinstance(raw_items, list):
        raise _RemoteChatProtocolError(
            f"remote chat {operation} returned invalid items"
        )
    items: list[dict[str, object]] = []
    for raw in raw_items:
        item = _mapping(raw, operation=f"{operation} item")
        required = "selector" if item_kind == "model" else "name"
        if not isinstance(item.get(required), str) or not cast(str, item[required]):
            raise _RemoteChatProtocolError(
                f"remote chat {operation} returned invalid items"
            )
        items.append(dict(item))
    return {"default": default, "items": items}


def _mapping(payload: object, *, operation: str) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise _RemoteChatProtocolError(f"remote chat {operation} returned invalid data")
    return cast(Mapping[str, object], payload)


def _label(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise _RemoteChatProtocolError(f"remote chat {label} is invalid")
    text = value.strip()
    if (
        not text
        or text != value
        or not text.isprintable()
        or any(character.isspace() for character in text)
        or any(character in text for character in ",()")
    ):
        raise _RemoteChatProtocolError(f"remote chat {label} is invalid")
    return text


def _token(value: object, *, label: str) -> str:
    text = _label(value, label=label)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", text) is None:
        raise _RemoteChatProtocolError(f"remote chat {label} is invalid")
    return text


def _run_override_data(command: RunOverride) -> dict[str, object]:
    value = command.value
    if isinstance(value, tuple):
        encoded: object = list(value)
    elif isinstance(value, Decimal):
        encoded = str(value)
    else:
        encoded = value
    return {
        "group": command.group,
        "field": command.field,
        "value": encoded,
    }


def _http_error(response: httpx.Response, *, operation: str) -> RemoteChatError:
    detail = response.reason_phrase or "request failed"
    try:
        payload = response.json()
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    else:
        if isinstance(payload, Mapping) and isinstance(payload.get("detail"), str):
            detail = cast(str, payload["detail"])
    return RemoteChatError(
        f"remote chat {operation} failed: HTTP {response.status_code} {detail}",
        status_code=response.status_code,
        detail=detail,
    )


def _emit_state(
    callback: Callable[[ChatRunState], None] | None,
    state: ChatRunState,
) -> None:
    if callback is not None:
        callback(state)


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


def _close_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    pending = {task for task in asyncio.all_tasks(loop) if not task.done()}
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.run_until_complete(loop.shutdown_default_executor())
    loop.close()
