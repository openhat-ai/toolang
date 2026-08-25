"""HTTP implementation of the transport-neutral run client boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
import json
import logging
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import httpx
from httpx_sse import SSEError, ServerSentEvent, aconnect_sse
from pydantic import TypeAdapter, ValidationError

from toolang.base.types.message import Message
from toolang.execution.client import RunHandle
from toolang.execution.events import RunBegin, RunEnd, RunTracer, run_event_from_data
from toolang.execution.schemas import ControlInfo, RunDetail, RunRequest
from toolang.execution.types import ControlTiming, RunOverride, validate_execution_id


_LOGGER = logging.getLogger(__name__)
_RUN_ID_HEADER = "X-Toolang-Run-ID"
_RUN_DETAIL_ADAPTER = TypeAdapter(RunDetail)
_CONTROL_INFO_ADAPTER = TypeAdapter(ControlInfo)


class RemoteRunClientError(RuntimeError):
    """One remote run transport or protocol failure."""

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


@dataclass(frozen=True, slots=True)
class _RemoteRunHandle:
    run_id: str
    _reader: asyncio.Task[None] = field(repr=False)
    _client: RemoteRunClient = field(repr=False)

    async def wait(self) -> RunDetail:
        try:
            await asyncio.shield(self._reader)
        except asyncio.CancelledError:
            if self._client.closed:
                raise RemoteRunClientError("remote run client is closed") from None
            raise
        return await self._client._run_detail(self.run_id)


class RemoteRunClient:
    """Start and control runs through one agent HTTP endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = _normalize_endpoint(endpoint)
        self._http = client or httpx.AsyncClient()
        self._owns_http = client is None
        self._readers: set[asyncio.Task[None]] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(
        self,
        request: RunRequest,
        *,
        tracer: RunTracer | None = None,
    ) -> RunHandle:
        self._require_open()
        loop = asyncio.get_running_loop()
        accepted: asyncio.Future[str] = loop.create_future()
        delivery = asyncio.Event()
        reader = asyncio.create_task(
            self._read_run_stream(
                request,
                tracer=tracer,
                accepted=accepted,
                delivery=delivery,
            ),
            name=f"toolang-remote-run-{request.request_id}",
        )
        self._readers.add(reader)
        reader.add_done_callback(self._reader_done)
        try:
            run_id = await accepted
        except BaseException:
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            raise
        handle = _RemoteRunHandle(
            run_id=run_id,
            _reader=reader,
            _client=self,
        )
        delivery.set()
        return handle

    async def stop(
        self,
        run_id: str,
        *,
        timing: ControlTiming = "immediate",
        request_id: str | None = None,
        reason: str | None = None,
    ) -> ControlInfo:
        validate_execution_id(run_id, label="run id")
        return await self._post_control(
            run_id,
            "cancel",
            payload={
                "mode": timing,
                "request_id": request_id,
                "reason": reason,
            },
        )

    async def steer(
        self,
        run_id: str,
        message: Message,
        *,
        timing: ControlTiming = "next_step",
        request_id: str | None = None,
    ) -> ControlInfo:
        validate_execution_id(run_id, label="run id")
        if message.role != "user":
            raise ValueError("run steer requires a user message")
        return await self._post_control(
            run_id,
            "steer",
            payload={
                "mode": timing,
                "request_id": request_id,
                "message": message.to_data(),
            },
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        readers = tuple(self._readers)
        for reader in readers:
            reader.cancel()
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)
        if self._owns_http:
            await self._http.aclose()

    async def _read_run_stream(
        self,
        request: RunRequest,
        *,
        tracer: RunTracer | None,
        accepted: asyncio.Future[str],
        delivery: asyncio.Event,
    ) -> None:
        try:
            async with aconnect_sse(
                self._http,
                "POST",
                self._url("/api/v1/runs/authored/stream"),
                json=_run_request_data(request),
                timeout=_stream_timeout(self._http.timeout),
            ) as source:
                response = source.response
                if not response.is_success:
                    await response.aread()
                    raise _http_error(response, operation="start")
                _require_event_stream(response)
                run_id = _response_run_id(response)
                accepted.set_result(run_id)
                await delivery.wait()
                await self._consume_events(source.aiter_sse(), run_id, tracer=tracer)
        except asyncio.CancelledError:
            if not accepted.done() and self._closed:
                accepted.set_exception(
                    RemoteRunClientError("remote run client is closed")
                )
            raise
        except RemoteRunClientError as exc:
            if not accepted.done():
                accepted.set_exception(exc)
            raise
        except httpx.HTTPError as exc:
            error = _transport_error("start", exc)
            if not accepted.done():
                accepted.set_exception(error)
            raise error from exc
        except (SSEError, ValidationError, ValueError) as exc:
            error = RemoteRunClientError("remote run start returned invalid data")
            if not accepted.done():
                accepted.set_exception(error)
            raise error from exc
        except Exception as exc:
            error = RemoteRunClientError(
                f"remote run start failed: {type(exc).__name__}"
            )
            if not accepted.done():
                accepted.set_exception(error)
            raise error from exc

    async def _consume_events(
        self,
        events: AsyncIterator[ServerSentEvent],
        run_id: str,
        *,
        tracer: RunTracer | None,
    ) -> None:
        first = True
        async for wire_event in events:
            try:
                data = json.loads(wire_event.data)
            except json.JSONDecodeError as exc:
                raise RemoteRunClientError(
                    "remote run stream returned invalid event JSON"
                ) from exc
            try:
                event = run_event_from_data(data)
            except (TypeError, ValueError) as exc:
                raise RemoteRunClientError(
                    "remote run stream returned an invalid run event"
                ) from exc
            if wire_event.event != event.type:
                raise RemoteRunClientError(
                    "remote run stream event name does not match its payload"
                )
            if first:
                if (
                    not isinstance(event, RunBegin)
                    or event.parent is not None
                    or event.run != run_id
                ):
                    raise RemoteRunClientError(
                        "remote run stream did not begin with the accepted root run"
                    )
                first = False
            elif isinstance(event, RunBegin) and event.parent is None:
                raise RemoteRunClientError(
                    "remote run stream returned a second root run"
                )
            if tracer is not None:
                try:
                    await tracer.on_event(event)
                except Exception:
                    _LOGGER.exception("remote run tracer event handling failed")
            if isinstance(event, RunEnd) and event.run == run_id:
                return
        raise RemoteRunClientError(
            f"remote run stream ended before root completion: {run_id}"
        )

    async def _run_detail(self, run_id: str) -> RunDetail:
        validate_execution_id(run_id, label="run id")
        response = await self._request(
            "GET",
            f"/api/v1/runs/{run_id}",
            operation="wait",
        )
        payload = _response_json(response, operation="wait")
        try:
            return _RUN_DETAIL_ADAPTER.validate_python(payload)
        except ValidationError as exc:
            raise RemoteRunClientError(
                "remote run wait returned invalid run detail"
            ) from exc

    async def _post_control(
        self,
        run_id: str,
        action: str,
        *,
        payload: dict[str, object],
    ) -> ControlInfo:
        response = await self._request(
            "POST",
            f"/api/v1/runs/{run_id}/{action}",
            operation=action,
            json=payload,
        )
        body = _response_json(response, operation=action)
        if not isinstance(body, dict) or "command" not in body:
            raise RemoteRunClientError(
                f"remote run {action} returned invalid control data"
            )
        control_body = cast(dict[str, object], body)
        try:
            return _CONTROL_INFO_ADAPTER.validate_python(control_body["command"])
        except ValidationError as exc:
            raise RemoteRunClientError(
                f"remote run {action} returned invalid control data"
            ) from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        **kwargs: Any,
    ) -> httpx.Response:
        self._require_open()
        try:
            response = await self._http.request(method, self._url(path), **kwargs)
        except (httpx.HTTPError, RuntimeError) as exc:
            raise _transport_error(operation, exc) from exc
        if not response.is_success:
            raise _http_error(response, operation=operation)
        return response

    def _url(self, path: str) -> str:
        return f"{self._endpoint}{path}"

    def _require_open(self) -> None:
        if self._closed:
            raise RemoteRunClientError("remote run client is closed")
        if self._http.is_closed:
            raise RemoteRunClientError("remote run HTTP client is closed")

    def _reader_done(self, reader: asyncio.Task[None]) -> None:
        self._readers.discard(reader)
        if not reader.cancelled():
            _ = reader.exception()


def _normalize_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str):
        raise TypeError("remote run endpoint must be a string")
    if (
        not endpoint
        or endpoint != endpoint.strip()
        or any(character.isspace() for character in endpoint)
        or "?" in endpoint
        or "#" in endpoint
    ):
        raise ValueError("remote run endpoint must be canonical")
    try:
        parsed = urlsplit(endpoint)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("remote run endpoint is invalid") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
    ):
        raise ValueError("remote run endpoint must be an absolute HTTP origin")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, "", "", ""))


def _run_request_data(request: RunRequest) -> dict[str, object]:
    return {
        "thread": request.thread,
        "request_id": request.request_id,
        "commands": [_run_override_data(item) for item in request.commands],
        "input": {
            "primary": request.input.primary,
            "named": [
                {"name": name, "source": source} for name, source in request.input.named
            ],
        },
        "session_commands": [
            _run_override_data(item) for item in request.session_commands
        ],
        "runnable_fallbacks": list(request.runnable_fallbacks),
    }


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


def _stream_timeout(timeout: httpx.Timeout) -> httpx.Timeout:
    return httpx.Timeout(
        connect=cast(float | None, timeout.connect),
        read=None,
        write=cast(float | None, timeout.write),
        pool=cast(float | None, timeout.pool),
    )


def _require_event_stream(response: httpx.Response) -> None:
    content_type = (
        response.headers.get("content-type", "").partition(";")[0].strip().lower()
    )
    if content_type != "text/event-stream":
        raise RemoteRunClientError("remote run start returned a non-SSE response")


def _response_run_id(response: httpx.Response) -> str:
    value = response.headers.get(_RUN_ID_HEADER)
    try:
        return validate_execution_id(value, label="accepted run id")
    except ValueError as exc:
        raise RemoteRunClientError(
            "remote run start returned an invalid accepted run ID"
        ) from exc


def _response_json(response: httpx.Response, *, operation: str) -> object:
    try:
        return response.json()
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteRunClientError(
            f"remote run {operation} returned invalid JSON"
        ) from exc


def _http_error(response: httpx.Response, *, operation: str) -> RemoteRunClientError:
    detail = response.reason_phrase or "request failed"
    try:
        payload = response.json()
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    else:
        if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
            detail = cast(str, payload["detail"])
    return RemoteRunClientError(
        f"remote run {operation} failed: HTTP {response.status_code} {detail}",
        status_code=response.status_code,
        detail=detail,
    )


def _transport_error(operation: str, error: Exception) -> RemoteRunClientError:
    return RemoteRunClientError(
        f"remote run {operation} transport failed: {type(error).__name__}"
    )
