"""HTTP implementation of the transport-neutral run client boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from ipaddress import ip_address
import json
import logging
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import httpx
from httpx_sse import SSEError, ServerSentEvent, aconnect_sse
from pydantic import TypeAdapter, ValidationError

from toolang.base.types.message import Message
from toolang.base.types.model import ModelRequest
from toolang.execution.client import RunHandle
from toolang.execution.events import RunBegin, RunEnd, RunTracer, run_event_from_data
from toolang.execution.schemas import (
    ControlInfo,
    RerunRequest,
    RetryRequest,
    RunDetail,
    RunRequest,
)
from toolang.execution.types import ControlTiming, RunCommand, validate_execution_id


_LOGGER = logging.getLogger(__name__)
_RUN_ID_HEADER = "X-Toolang-Run-ID"
_RUN_DETAIL_ADAPTER = TypeAdapter(RunDetail)
_CONTROL_INFO_ADAPTER = TypeAdapter(ControlInfo)
_RUN_REQUEST_ADAPTER = TypeAdapter(RunRequest)
_MODEL_REQUEST_ADAPTER = TypeAdapter(ModelRequest)


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
            if not self._client.connected:
                raise RemoteRunClientError(
                    "remote run client is disconnected"
                ) from None
            raise
        return await self._client._run_detail(self.run_id)


class RemoteRunClient:
    """Run and control runs through one agent HTTP endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = _normalize_endpoint(endpoint)
        self._http = client
        self._owns_http = client is None
        self._readers: set[asyncio.Task[None]] = set()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def endpoint(self) -> str:
        """Return the normalized HTTP origin used by this client."""

        return self._endpoint

    async def connect(self) -> None:
        if self._connected:
            self._require_connected()
            return
        if self._owns_http:
            self._http = httpx.AsyncClient()
        elif self._http is None or self._http.is_closed:
            raise RemoteRunClientError("remote run HTTP client is closed")
        self._connected = True

    async def run(
        self,
        request: RunRequest,
        *,
        tracer: RunTracer | None = None,
    ) -> RunHandle:
        return await self._submit_stream(
            operation="run",
            path="/api/v1/runs/authored/stream",
            payload=_run_request_data(request),
            request_id=request.request_id,
            source_run_id=None,
            tracer=tracer,
        )

    async def retry(
        self,
        request: RetryRequest,
        *,
        tracer: RunTracer | None = None,
    ) -> RunHandle:
        return await self._submit_stream(
            operation="retry",
            path=f"/api/v1/runs/{request.source}/retry/stream",
            payload=_restart_request_data(request),
            request_id=request.request_id,
            source_run_id=request.source,
            tracer=tracer,
        )

    async def rerun(
        self,
        request: RerunRequest,
        *,
        tracer: RunTracer | None = None,
    ) -> RunHandle:
        return await self._submit_stream(
            operation="rerun",
            path=f"/api/v1/runs/{request.source}/rerun/stream",
            payload=_restart_request_data(request),
            request_id=request.request_id,
            source_run_id=request.source,
            tracer=tracer,
        )

    async def _submit_stream(
        self,
        *,
        operation: str,
        path: str,
        payload: dict[str, object],
        request_id: str,
        source_run_id: str | None,
        tracer: RunTracer | None,
    ) -> RunHandle:
        self._require_connected()
        loop = asyncio.get_running_loop()
        accepted: asyncio.Future[str] = loop.create_future()
        delivery = asyncio.Event()
        reader = asyncio.create_task(
            self._read_run_stream(
                operation=operation,
                path=path,
                payload=payload,
                source_run_id=source_run_id,
                tracer=tracer,
                accepted=accepted,
                delivery=delivery,
            ),
            name=f"toolang-remote-{operation}-{request_id}",
        )
        self._readers.add(reader)
        reader.add_done_callback(self._reader_done)
        try:
            run_id = await self._wait_for_acceptance(accepted, reader)
        except BaseException:
            if not accepted.done():
                accepted.cancel()
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

    async def cancel(
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

    async def disconnect(self) -> None:
        if not self._connected:
            return
        self._connected = False
        readers = tuple(self._readers)
        for reader in readers:
            reader.cancel()
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    async def _wait_for_acceptance(
        self,
        accepted: asyncio.Future[str],
        reader: asyncio.Task[None],
    ) -> str:
        done, _pending = await asyncio.wait(
            (accepted, reader),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if accepted in done:
            return accepted.result()
        if reader.cancelled():
            raise RemoteRunClientError("remote run client is disconnected")
        await reader
        raise RemoteRunClientError("remote run ended before acceptance")

    async def _read_run_stream(
        self,
        *,
        operation: str,
        path: str,
        payload: dict[str, object],
        source_run_id: str | None,
        tracer: RunTracer | None,
        accepted: asyncio.Future[str],
        delivery: asyncio.Event,
    ) -> None:
        try:
            http = self._require_connected()
            async with aconnect_sse(
                http,
                "POST",
                self._url(path),
                json=payload,
                timeout=_stream_timeout(http.timeout),
            ) as source:
                response = source.response
                if not response.is_success:
                    await response.aread()
                    raise _http_error(response, operation=operation)
                _require_event_stream(response)
                run_id = _response_run_id(response)
                if operation == "retry" and run_id != source_run_id:
                    raise RemoteRunClientError(
                        "remote retry did not accept the source run ID"
                    )
                if operation == "rerun" and run_id == source_run_id:
                    raise RemoteRunClientError(
                        "remote rerun did not accept a new run ID"
                    )
                accepted.set_result(run_id)
                await delivery.wait()
                await self._consume_events(source.aiter_sse(), run_id, tracer=tracer)
        except asyncio.CancelledError:
            if not accepted.done() and not self._connected:
                accepted.set_exception(
                    RemoteRunClientError("remote run client is disconnected")
                )
            raise
        except RemoteRunClientError as exc:
            if not accepted.done():
                accepted.set_exception(exc)
            raise
        except httpx.HTTPError as exc:
            error = _transport_error(operation, exc)
            if not accepted.done():
                accepted.set_exception(error)
            raise error from exc
        except (SSEError, ValidationError, ValueError) as exc:
            error = RemoteRunClientError("remote run returned invalid data")
            if not accepted.done():
                accepted.set_exception(error)
            raise error from exc
        except Exception as exc:
            error = RemoteRunClientError(f"remote run failed: {type(exc).__name__}")
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
            detail = _RUN_DETAIL_ADAPTER.validate_python(payload)
        except ValidationError as exc:
            raise RemoteRunClientError(
                "remote run wait returned invalid run detail"
            ) from exc
        if (
            detail.id != run_id
            or detail.root_run_id != run_id
            or detail.parent is not None
            or detail.status not in {"succeeded", "failed", "canceled"}
        ):
            raise RemoteRunClientError("remote run wait returned invalid run detail")
        return detail

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
            control = _CONTROL_INFO_ADAPTER.validate_python(control_body["command"])
        except ValidationError as exc:
            raise RemoteRunClientError(
                f"remote run {action} returned invalid control data"
            ) from exc
        expected_kind = "cancel" if action == "cancel" else "steer"
        if control.run_id != run_id or control.kind != expected_kind:
            raise RemoteRunClientError(
                f"remote run {action} returned invalid control data"
            )
        return control

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        **kwargs: Any,
    ) -> httpx.Response:
        http = self._require_connected()
        try:
            response = await http.request(method, self._url(path), **kwargs)
        except (httpx.HTTPError, RuntimeError) as exc:
            raise _transport_error(operation, exc) from exc
        if not response.is_success:
            raise _http_error(response, operation=operation)
        return response

    def _url(self, path: str) -> str:
        return f"{self._endpoint}{path}"

    def _require_connected(self) -> httpx.AsyncClient:
        if not self._connected:
            raise RemoteRunClientError("remote run client is disconnected")
        http = self._http
        if http is None or http.is_closed:
            raise RemoteRunClientError("remote run HTTP client is closed")
        return http

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
        or any(
            character.isspace() or not character.isprintable() for character in endpoint
        )
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
        or not _valid_endpoint_host(parsed.hostname)
    ):
        raise ValueError("remote run endpoint must be an absolute HTTP origin")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, "", "", ""))


def _valid_endpoint_host(host: str) -> bool:
    if ":" in host:
        try:
            ip_address(host)
        except ValueError:
            return False
        return True
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if ascii_host.endswith("."):
        ascii_host = ascii_host[:-1]
    labels = ascii_host.split(".")
    return (
        bool(ascii_host)
        and len(ascii_host) <= 253
        and all(
            0 < len(label) <= 63
            and label[0].isalnum()
            and label[-1].isalnum()
            and all(character.isalnum() or character == "-" for character in label)
            for label in labels
        )
    )


def _run_request_data(request: RunRequest) -> dict[str, object]:
    return cast(
        dict[str, object],
        _RUN_REQUEST_ADAPTER.dump_python(request, mode="json"),
    )


def _restart_request_data(request: RetryRequest | RerunRequest) -> dict[str, object]:
    payload: dict[str, object] = {
        "request_id": request.request_id,
        "commands": [_run_command_data(item) for item in request.commands],
    }
    if isinstance(request, RetryRequest):
        payload["anchor"] = str(request.anchor) if request.anchor is not None else None
    elif request.model is not None:
        payload["model"] = _MODEL_REQUEST_ADAPTER.dump_python(
            request.model,
            mode="json",
        )
    return payload


def _run_command_data(command: RunCommand) -> dict[str, object]:
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
        raise RemoteRunClientError("remote run returned a non-SSE response")


def _response_run_id(response: httpx.Response) -> str:
    value = response.headers.get(_RUN_ID_HEADER)
    try:
        return validate_execution_id(value, label="accepted run id")
    except ValueError as exc:
        raise RemoteRunClientError(
            "remote run returned an invalid accepted run ID"
        ) from exc


def _response_json(response: httpx.Response, *, operation: str) -> object:
    try:
        return response.json()
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteRunClientError(
            f"{_operation_label(operation)} returned invalid JSON"
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
        f"{_operation_label(operation)} failed: HTTP {response.status_code} {detail}",
        status_code=response.status_code,
        detail=detail,
    )


def _transport_error(operation: str, error: Exception) -> RemoteRunClientError:
    return RemoteRunClientError(
        f"{_operation_label(operation)} transport failed: {type(error).__name__}"
    )


def _operation_label(operation: str) -> str:
    return "remote run" if operation == "run" else f"remote run {operation}"
