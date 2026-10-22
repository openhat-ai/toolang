"""HTTP implementation of the transport-neutral run client boundary."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from ipaddress import ip_address
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from httpx_sse import ServerSentEvent, SSEError, aconnect_sse
from pydantic import TypeAdapter, ValidationError

from toolang.api.version import AGENT_API_VERSION
from toolang.base.money import cost_text
from toolang.base.types.message import Message
from toolang.base.types.model import ModelOverride, ModelRequest
from toolang.execution.client import RunHandle
from toolang.execution.events import (
    RunBegin,
    RunSnapshot,
    RunTracer,
    ThreadCreated,
    ThreadForked,
    ThreadRewound,
)
from toolang.execution.stream_client import StreamClientState
from toolang.execution.schemas import StreamFrame
from toolang.execution.schemas import (
    ControlInfo,
    LiveSnapshot,
    RerunRequest,
    RunDetail,
    RunRequest,
    ThreadInfo,
)
from toolang.execution.types import (
    ControlRef,
    ControlTiming,
    RunCommand,
    RunRef,
    ThreadRef,
)

_LOGGER = logging.getLogger(__name__)
_RUN_ID_HEADER = "X-Toolang-Run-ID"
_RUN_DETAIL_ADAPTER = TypeAdapter(RunDetail)
_LIVE_SNAPSHOT_ADAPTER = TypeAdapter(LiveSnapshot)
_CONTROL_INFO_ADAPTER = TypeAdapter(ControlInfo)
_THREAD_INFO_ADAPTER = TypeAdapter(ThreadInfo)
_RUN_REQUEST_ADAPTER = TypeAdapter(RunRequest)
_MODEL_REQUEST_ADAPTER = TypeAdapter(ModelRequest)
_MODEL_OVERRIDE_ADAPTER = TypeAdapter(ModelOverride)


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


class _InterruptedStream(RemoteRunClientError):
    """A recoverable stream failure after a committed cursor."""


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
        except RemoteRunClientError as exc:
            _LOGGER.error(
                "remote run wait failed run=%s phase=stream error=%s status=%s cause=%s",
                self.run_id,
                type(exc).__name__,
                exc.status_code,
                type(exc.__cause__).__name__ if exc.__cause__ else None,
            )
            raise
        self._client._require_connected()
        detail = asyncio.create_task(self._client._run_detail(self.run_id))
        self._client._details.add(detail)
        try:
            return await detail
        except asyncio.CancelledError:
            if not self._client.connected:
                raise RemoteRunClientError(
                    "remote run client is disconnected"
                ) from None
            raise
        except RemoteRunClientError as exc:
            _LOGGER.error(
                "remote run wait failed run=%s phase=detail error=%s status=%s cause=%s",
                self.run_id,
                type(exc).__name__,
                exc.status_code,
                type(exc.__cause__).__name__ if exc.__cause__ else None,
            )
            raise
        finally:
            self._client._details.discard(detail)


class RemoteRunClient:
    """Run and control runs through one agent HTTP endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient | None = None,
        interactive: bool = False,
        client_id: str | None = None,
        guarded: bool = False,
    ) -> None:
        self._endpoint = _normalize_endpoint(endpoint)
        self._interactive = interactive
        self._guarded = guarded
        self.client_id = client_id or f"client_{uuid4().hex}"
        self._http = client
        self._owns_http = client is None
        self._readers: set[asyncio.Task[None]] = set()
        self._details: set[asyncio.Task[RunDetail]] = set()
        self._connection_lock = asyncio.Lock()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def endpoint(self) -> str:
        """Return the normalized HTTP origin used by this client."""

        return self._endpoint

    async def connect(self) -> None:
        async with self._connection_lock:
            if self._connected:
                self._require_connected()
                return
            if self._owns_http:
                self._http = httpx.AsyncClient()
            elif self._http is None or self._http.is_closed:
                raise RemoteRunClientError("remote run HTTP client is closed")
            self._connected = True

    async def live_trees(self, *, root_run_id: str | None = None) -> LiveSnapshot:
        self._require_connected()
        if root_run_id is not None:
            RunRef.parse(root_run_id)
        response = await self._request(
            "GET",
            f"/api/v{AGENT_API_VERSION}/runs/live",
            operation="live trees",
            params={"root_run_id": root_run_id} if root_run_id is not None else None,
        )
        payload = _response_json(response, operation="live trees")
        try:
            snapshot = _LIVE_SNAPSHOT_ADAPTER.validate_python(payload)
        except ValidationError as exc:
            raise RemoteRunClientError(
                "remote live trees returned invalid snapshot"
            ) from exc
        if root_run_id is not None and any(
            tree.root_run_id != root_run_id for tree in snapshot.trees
        ):
            raise RemoteRunClientError("remote live trees returned an unexpected root")
        return snapshot

    async def run(
        self,
        request: RunRequest,
        *,
        tracer: RunTracer | None = None,
    ) -> RunHandle:
        return await self._submit_stream(
            operation="run",
            path=f"/api/v{AGENT_API_VERSION}/runs/authored/stream",
            payload=_run_request_data(
                replace(request, client_id=request.client_id or self.client_id)
            ),
            request_id=request.request_id,
            source_run_id=None,
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
            path=f"/api/v{AGENT_API_VERSION}/runs/{request.source}/rerun/stream",
            payload=_restart_request_data(
                replace(request, client_id=request.client_id or self.client_id)
            ),
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
        if self._interactive:
            path = path.replace(
                f"/api/v{AGENT_API_VERSION}/runs/",
                f"/api/v{AGENT_API_VERSION}/chat/runs/",
                1,
            )
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
        expected_control: ControlRef | None = None,
        reason: str | None = None,
    ) -> ControlInfo:
        RunRef.parse(run_id)
        if self._guarded and expected_control is None:
            invocation = (
                await self._fetch_run_detail(run_id, operation="cancel")
            ).control
            expected_control = ControlRef.for_run(invocation.run, invocation.index)
        return await self._post_control(
            run_id,
            "cancel",
            payload={
                "mode": timing,
                "request_id": request_id,
                "client_id": self.client_id,
                **(
                    {"expected_control": str(expected_control)}
                    if expected_control is not None
                    else {}
                ),
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
        expected_control: ControlRef | None = None,
    ) -> ControlInfo:
        RunRef.parse(run_id)
        if self._guarded and expected_control is None:
            invocation = (
                await self._fetch_run_detail(run_id, operation="steer")
            ).control
            expected_control = ControlRef.for_run(invocation.run, invocation.index)
        if message.role != "user":
            raise ValueError("run steer requires a user message")
        return await self._post_control(
            run_id,
            "steer",
            payload={
                "mode": timing,
                "request_id": request_id,
                "client_id": self.client_id,
                **(
                    {"expected_control": str(expected_control)}
                    if expected_control is not None
                    else {}
                ),
                "message": message.to_data(),
            },
        )

    async def disconnect(self) -> None:
        async with self._connection_lock:
            if not self._connected:
                return
            self._connected = False
            pending = (*self._readers, *self._details)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if self._owns_http and self._http is not None:
                await self._http.aclose()

    async def fork_thread(self, thread_id: str, *, run_id: str) -> ThreadInfo:
        """Fork through a recorded run using the owning agent."""

        return await self._mutate_thread(thread_id, "fork", run_id=run_id)

    async def rewind_thread(self, thread_id: str, *, run_id: str) -> ThreadInfo:
        """Rewind before a recorded run using the owning agent."""

        return await self._mutate_thread(thread_id, "rewind", run_id=run_id)

    async def _mutate_thread(
        self, thread_id: str, action: str, *, run_id: str
    ) -> ThreadInfo:
        ThreadRef.parse(thread_id)
        RunRef.parse(run_id)
        response = await self._request(
            "POST",
            f"/api/v{AGENT_API_VERSION}/threads/{thread_id}/{action}",
            operation=action,
            json={"run_id": run_id},
        )
        body = _response_json(response, operation=action)
        try:
            thread = _THREAD_INFO_ADAPTER.validate_python(
                cast(dict[str, object], body).get("thread")
                if isinstance(body, dict)
                else None
            )
        except ValidationError as exc:
            raise RemoteRunClientError(
                f"remote thread {action} returned invalid thread data"
            ) from exc
        if (thread.id == thread_id) != (action == "rewind"):
            raise RemoteRunClientError(
                f"remote thread {action} returned an unexpected thread"
            )
        return thread

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
        state = StreamClientState()
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
                events = source.aiter_sse()
                if _RUN_ID_HEADER in response.headers:
                    run_id = _response_run_id(response)
                else:
                    try:
                        first_event = await anext(events)
                        first_data = json.loads(first_event.data)
                        if (
                            not isinstance(first_data, dict)
                            or first_event.event != "run_begin"
                            or first_data.get("type") != first_event.event
                            or first_data.get("parent") is not None
                        ):
                            raise ValueError("first event has no accepted root")
                        run_id = str(RunRef.parse(first_data.get("run")))
                    except (StopAsyncIteration, TypeError, ValueError) as exc:
                        raise RemoteRunClientError(
                            "remote run returned an invalid accepted run ID"
                        ) from exc
                    events = _prepend_event(first_event, events)
                if operation == "rerun" and run_id == source_run_id:
                    raise RemoteRunClientError(
                        "remote rerun did not accept a new run ID"
                    )
                accepted.set_result(run_id)
                await delivery.wait()
                try:
                    await self._consume_events(
                        events, run_id, tracer=tracer, state=state
                    )
                    return
                except (httpx.HTTPError, _InterruptedStream) as exc:
                    _LOGGER.warning(
                        "remote run stream interrupted run=%s phase=initial error=%s",
                        run_id,
                        type(exc).__name__,
                    )
            # Never resubmit POST. A known run ID is enough; without a committed
            # cursor, GET initializes its entire recorded tree. A cursor permits
            # a smaller catchup independently of the admission request.
            for attempt, delay in enumerate((0.1, 0.5, 1.0)):
                await asyncio.sleep(delay)
                await self._present(tracer, state.attach())
                try:
                    async with aconnect_sse(
                        http,
                        "GET",
                        self._url(f"/api/v{AGENT_API_VERSION}/runs/{run_id}/stream"),
                        params={"after": state.cursor}
                        if state.cursor is not None
                        else {},
                        timeout=_stream_timeout(http.timeout),
                    ) as resumed:
                        if not resumed.response.is_success:
                            await resumed.response.aread()
                            raise _http_error(resumed.response, operation="resume")
                        _require_event_stream(resumed.response)
                        await self._consume_events(
                            resumed.aiter_sse(),
                            run_id,
                            tracer=tracer,
                            state=state,
                            resumed=True,
                        )
                        return
                except (
                    httpx.HTTPError,
                    _InterruptedStream,
                    RemoteRunClientError,
                ) as exc:
                    _LOGGER.warning(
                        "remote run stream interrupted run=%s phase=resume attempt=%s/3 error=%s status=%s",
                        run_id,
                        attempt + 1,
                        type(exc).__name__,
                        exc.status_code
                        if isinstance(exc, RemoteRunClientError)
                        else None,
                    )
                    if attempt == 2 or not isinstance(
                        exc, (httpx.HTTPError, _InterruptedStream)
                    ):
                        raise
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
        state: StreamClientState | None = None,
        resumed: bool = False,
    ) -> None:
        state = state or StreamClientState()
        first = not resumed
        root_seen = False
        async for wire in events:
            # httpx-sse retains the last event ID and can emit a default empty
            # "message" for a comment-only keep-alive following an identified event.
            # It is not a run event and must not advance the committed cursor.
            if wire.event == "message" and not wire.data:
                continue
            try:
                data = json.loads(wire.data)
            except json.JSONDecodeError as exc:
                _LOGGER.warning(
                    "remote run stream error run=%s phase=event reason=invalid_json",
                    run_id,
                )
                raise RemoteRunClientError(
                    "remote run stream returned invalid event JSON"
                ) from exc
            if not isinstance(data, dict):
                label = "stream error" if wire.event == "stream_error" else "run event"
                raise RemoteRunClientError(
                    f"remote run stream returned an invalid {label}"
                )
            if wire.event == "stream_error":
                code = data.get("code")
                _LOGGER.warning(
                    "remote run stream error run=%s phase=event code=%s",
                    run_id,
                    code if code in ("overflow", "snapshot_limit") else "other",
                )
                if code == "overflow":
                    raise _InterruptedStream(
                        f"remote run subscription overflow: {run_id}"
                    )
                raise RemoteRunClientError(f"remote run stream error: {code}")
            if (
                root_seen
                and not resumed
                and wire.event == "run_begin"
                and data.get("parent") is None
                and not data.get("context")
                and not data.get("cursor")
            ):
                raise RemoteRunClientError(
                    "remote run stream returned a second root run"
                )
            try:
                observations = state.feed(
                    StreamFrame(wire.event, data, wire.id or None)
                )
            except (TypeError, ValueError) as exc:
                raise RemoteRunClientError(
                    f"remote run stream returned invalid data: {exc}"
                ) from exc
            for event in observations:
                if isinstance(event, RunSnapshot):
                    roots = [
                        item
                        for item in event.events
                        if isinstance(item, RunBegin) and item.parent is None
                    ]
                    if any(item.run != run_id for item in roots):
                        raise RemoteRunClientError(
                            "remote prefill returned an unexpected root run"
                        )
                    if roots:
                        first = False
                    await self._present(tracer, event)
                    continue
                if isinstance(event, ThreadCreated | ThreadForked | ThreadRewound):
                    raise RemoteRunClientError(
                        "remote run stream returned a thread event"
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
                if isinstance(event, RunBegin) and event.parent is None:
                    if event.run != run_id or root_seen:
                        raise RemoteRunClientError(
                            "remote run stream returned a second root run"
                        )
                    root_seen = True
                await self._present(tracer, event)
        if not state.complete(run_id):
            raise _InterruptedStream(
                f"remote run stream ended before root completion: {run_id}"
            )

    @staticmethod
    async def _present(tracer: RunTracer | None, event) -> None:
        if tracer is None:
            return
        try:
            if isinstance(event, RunSnapshot):
                await tracer.on_snapshot(event.events)
            else:
                await tracer.on_event(event)
        except Exception:
            _LOGGER.exception("remote run tracer event handling failed")

    async def _fetch_run_detail(self, run_id: str, *, operation: str) -> RunDetail:
        RunRef.parse(run_id)
        response = await self._request(
            "GET",
            f"/api/v{AGENT_API_VERSION}/runs/{run_id}",
            operation=operation,
        )
        payload = _response_json(response, operation=operation)
        try:
            detail = _RUN_DETAIL_ADAPTER.validate_python(payload)
        except ValidationError as exc:
            raise RemoteRunClientError(
                f"remote run {operation} returned invalid run detail"
            ) from exc
        if detail.id != run_id:
            raise RemoteRunClientError(
                f"remote run {operation} returned invalid run detail"
            )
        return detail

    async def _run_detail(self, run_id: str) -> RunDetail:
        detail = await self._fetch_run_detail(run_id, operation="wait")
        if (
            detail.root_run_id != run_id
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
        prefix = (
            f"/api/v{AGENT_API_VERSION}/chat/runs"
            if self._interactive and action == "steer"
            else f"/api/v{AGENT_API_VERSION}/runs"
        )
        response = await self._request(
            "POST",
            f"{prefix}/{run_id}/{action}",
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


async def _prepend_event(
    first: ServerSentEvent, events: AsyncIterator[ServerSentEvent]
) -> AsyncIterator[ServerSentEvent]:
    yield first
    async for event in events:
        yield event


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
        _RUN_REQUEST_ADAPTER.dump_python(
            request,
            mode="json",
            exclude={
                name
                for name in ("attachments", "source_revision", "driver")
                if getattr(request, name) is None
            },
        ),
    )


def _restart_request_data(request: RerunRequest) -> dict[str, object]:
    payload: dict[str, object] = {
        "request_id": request.request_id,
        "client_id": request.client_id,
        "commands": [_run_command_data(item) for item in request.commands],
    }
    if request.model is not None:
        payload["model"] = _MODEL_REQUEST_ADAPTER.dump_python(
            request.model,
            mode="json",
        )
    elif request.model_override is not None:
        payload["model_override"] = _MODEL_OVERRIDE_ADAPTER.dump_python(
            request.model_override,
            mode="json",
        )
    return payload


def _run_command_data(command: RunCommand) -> dict[str, object]:
    value = command.value
    if isinstance(value, tuple):
        encoded: object = list(value)
    elif command.group == "limit" and command.field == "cost" and value is not None:
        encoded = cost_text(cast(float, value))
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
        return str(RunRef.parse(value))
    except (TypeError, ValueError) as exc:
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
