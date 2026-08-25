"""HTTP run client transport, protocol, and lifecycle behavior."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
import json

import httpx
import pytest
from pydantic import TypeAdapter

from toolang.base.types.message import Message
from toolang.execution.events import (
    RunBegin,
    RunEnd,
    RunEvent,
    RunTracer,
    run_event_to_data,
)
from toolang.execution.records import SteerControlPayload, StopControlPayload
from toolang.execution.remote import RemoteRunClient, RemoteRunClientError
from toolang.execution.schemas import (
    ControlInfo,
    RunControlRefData,
    RunDetail,
    RunRequest,
)
from toolang.execution.types import ControlRef, Local, RunOverride
from toolang.lang.input import RunnableInputRaw


_DETAIL_ADAPTER = TypeAdapter(RunDetail)
_CONTROL_ADAPTER = TypeAdapter(ControlInfo)


class _Bytes(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes, wait: asyncio.Event | None = None) -> None:
        self._chunks = chunks
        self._wait = wait

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._wait is not None:
            await self._wait.wait()


@dataclass
class _Tracer(RunTracer):
    events: list[RunEvent] = field(default_factory=list)
    fail: bool = False

    async def on_event(self, event: RunEvent) -> None:
        self.events.append(event)
        if self.fail:
            raise RuntimeError("presenter failed")


@dataclass
class _Transport:
    handler: Callable[[httpx.Request], Awaitable[httpx.Response]]
    requests: list[tuple[str, str, object | None]] = field(default_factory=list)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        payload = json.loads(body) if body else None
        self.requests.append((request.method, str(request.url), payload))
        return await self.handler(request)


def _request() -> RunRequest:
    return RunRequest(
        thread="term_test",
        commands=(RunOverride("limit", "cost", Decimal("2.50")),),
        input=RunnableInputRaw(
            primary="hello",
            named=(("tone", "brief"),),
        ),
        session_commands=(
            RunOverride("allow", "models", ("openai/*",)),
            RunOverride("default", "runnable", "agic:chat"),
        ),
        runnable_fallbacks=("agic:chat", "default"),
        request_id="term_request",
    )


def _begin(run_id: str = "run_remote") -> RunBegin:
    return RunBegin(
        run=run_id,
        control=ControlRef(run_id, 0),
        runnable="agic:chat",
        started_at="2026-08-25T00:00:00Z",
    )


def _end(run_id: str = "run_remote") -> RunEnd:
    return RunEnd(
        run=run_id,
        status="succeeded",
        finished_at="2026-08-25T00:00:01Z",
    )


def _detail(run_id: str = "run_remote") -> RunDetail:
    return RunDetail(
        id=run_id,
        parent=None,
        thread_id="term_test",
        root_run_id=run_id,
        runnable_kind="agic",
        runnable_name="chat",
        call_kind="top",
        occurrence=None,
        input_text="hello",
        summary="done",
        status="succeeded",
        error=None,
        ejected=None,
        created_at="2026-08-25T00:00:00Z",
        started_at="2026-08-25T00:00:00Z",
        finished_at="2026-08-25T00:00:01Z",
        updated_at="2026-08-25T00:00:01Z",
        control=RunControlRefData(run=run_id, index=0),
        output=None,
        controls=[],
        steps=[],
    )


def _control(action: str, run_id: str = "run_remote") -> ControlInfo:
    if action == "cancel":
        payload = StopControlPayload((Local.typed("Text", "finished", "_", 0),))
        kind = "stop"
        timing = "immediate"
    else:
        payload = SteerControlPayload((Local.typed("Part[]", (), "_", 0),))
        kind = "steer"
        timing = "next_step"
    return ControlInfo(
        run_id=run_id,
        index=1,
        kind=kind,
        timing=timing,
        request_id=f"{action}_request",
        status="pending",
        payload=payload,
        error=None,
        created_at="2026-08-25T00:00:00Z",
        finished_at=None,
    )


def _event_stream(*events: RunEvent) -> _Bytes:
    chunks = []
    for event in events:
        data = json.dumps(run_event_to_data(event))
        chunks.append(f"event: {event.type}\ndata: {data}\n\n".encode())
    return _Bytes(*chunks)


def _stream_response(
    *events: RunEvent,
    run_id: str = "run_remote",
    headers: dict[str, str] | None = None,
    stream: httpx.AsyncByteStream | None = None,
) -> httpx.Response:
    return httpx.Response(
        200,
        headers={
            "content-type": "text/event-stream",
            "X-Toolang-Run-ID": run_id,
            **(headers or {}),
        },
        stream=stream or _event_stream(*events),
    )


def test_remote_client_starts_traces_and_waits_for_detail() -> None:
    detail_reads = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal detail_reads
        if request.method == "POST":
            return _stream_response(_begin(), _end())
        detail_reads += 1
        return httpx.Response(
            200,
            json=_DETAIL_ADAPTER.dump_python(_detail(), mode="json"),
        )

    async def scenario() -> None:
        transport = _Transport(handler)
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(transport),
            base_url="http://ignored.invalid/base/",
        )
        client = RemoteRunClient("http://runtime.test/", client=http)
        tracer = _Tracer()

        handle = await client.start(_request(), tracer=tracer)

        assert handle.run_id == "run_remote"
        assert tracer.events == []
        first = await handle.wait()
        second = await handle.wait()

        assert first == second == _detail()
        assert [event.type for event in tracer.events] == ["run_begin", "run_end"]
        assert detail_reads == 2
        assert transport.requests[0] == (
            "POST",
            "http://runtime.test/api/v1/runs/authored/stream",
            {
                "thread": "term_test",
                "request_id": "term_request",
                "commands": [{"group": "limit", "field": "cost", "value": "2.50"}],
                "input": {
                    "primary": "hello",
                    "named": [{"name": "tone", "source": "brief"}],
                },
                "session_commands": [
                    {
                        "group": "allow",
                        "field": "models",
                        "value": ["openai/*"],
                    },
                    {
                        "group": "default",
                        "field": "runnable",
                        "value": "agic:chat",
                    },
                ],
                "runnable_fallbacks": ["agic:chat", "default"],
            },
        )

        await client.close()
        await client.close()
        assert not http.is_closed
        await http.aclose()

    asyncio.run(scenario())


def test_remote_client_maps_stop_and_empty_steer_controls() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "run": _DETAIL_ADAPTER.dump_python(_detail(), mode="json"),
                "command": _CONTROL_ADAPTER.dump_python(
                    _control(action),
                    mode="json",
                ),
            },
        )

    async def scenario() -> None:
        transport = _Transport(handler)
        http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        client = RemoteRunClient("https://runtime.test", client=http)

        stop = await client.stop(
            "run_remote",
            timing="immediate",
            request_id="cancel_request",
            reason="finished",
        )
        steer = await client.steer(
            "run_remote",
            Message(role="user", parts=()),
            request_id="steer_request",
        )

        assert stop == _control("cancel")
        assert steer == _control("steer")
        assert transport.requests == [
            (
                "POST",
                "https://runtime.test/api/v1/runs/run_remote/cancel",
                {
                    "mode": "immediate",
                    "request_id": "cancel_request",
                    "reason": "finished",
                },
            ),
            (
                "POST",
                "https://runtime.test/api/v1/runs/run_remote/steer",
                {
                    "mode": "next_step",
                    "request_id": "steer_request",
                    "message": {"role": "user", "parts": []},
                },
            ),
        ]

        await client.close()
        await http.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        " runtime.test",
        "runtime.test",
        "ftp://runtime.test",
        "http://user@runtime.test",
        "http://run time.test",
        "http://runtime.test/api",
        "http://runtime.test?",
        "http://runtime.test?debug=1",
        "http://runtime.test#",
        "http://runtime.test#fragment",
    ],
)
def test_remote_client_rejects_invalid_endpoints(endpoint: str) -> None:
    with pytest.raises(ValueError):
        RemoteRunClient(endpoint)


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (
            _stream_response(_begin("run_other"), run_id="run_remote"),
            "did not begin with the accepted root run",
        ),
        (
            _stream_response(_begin(), stream=_Bytes(b"data: not-json\n\n")),
            "invalid event JSON",
        ),
        (
            _stream_response(
                stream=_Bytes(
                    (
                        "event: run_end\n"
                        f"data: {json.dumps(run_event_to_data(_begin()))}\n\n"
                    ).encode()
                )
            ),
            "event name does not match",
        ),
        (
            _stream_response(_begin(), _begin()),
            "second root run",
        ),
        (
            _stream_response(_begin()),
            "ended before root completion",
        ),
        (
            httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"ok": True},
            ),
            "non-SSE response",
        ),
    ],
)
def test_remote_client_rejects_invalid_stream_protocol(
    response: httpx.Response,
    error: str,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return response

    async def scenario() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = RemoteRunClient("http://runtime.test", client=http)

        if "non-SSE" in error:
            with pytest.raises(RemoteRunClientError, match=error):
                await client.start(_request())
        else:
            handle = await client.start(_request())
            with pytest.raises(RemoteRunClientError, match=error):
                await handle.wait()

        await client.close()
        await http.aclose()

    asyncio.run(scenario())


def test_remote_client_does_not_retry_or_expose_transport_endpoint() -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise httpx.ConnectError(
            "failed to connect to https://runtime.test/private",
            request=request,
        )

    async def scenario() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = RemoteRunClient("https://runtime.test", client=http)

        with pytest.raises(RemoteRunClientError) as caught:
            await client.start(_request())

        assert str(caught.value) == ("remote run start transport failed: ConnectError")
        assert "runtime.test" not in str(caught.value)
        assert requests == 1
        await client.close()
        await http.aclose()

    asyncio.run(scenario())


def test_remote_client_preserves_http_error_detail() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "Runnable not found: chat"})

    async def scenario() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = RemoteRunClient("http://runtime.test", client=http)

        with pytest.raises(RemoteRunClientError) as caught:
            await client.start(_request())

        assert caught.value.status_code == 422
        assert caught.value.detail == "Runnable not found: chat"
        assert str(caught.value) == (
            "remote run start failed: HTTP 422 Runnable not found: chat"
        )
        await client.close()
        await http.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("detail_response", "error"),
    [
        (httpx.Response(200, content=b"{"), "wait returned invalid JSON"),
        (httpx.Response(200, json={"id": "run_remote"}), "invalid run detail"),
    ],
)
def test_remote_client_rejects_invalid_run_detail(
    detail_response: httpx.Response,
    error: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _stream_response(_begin(), _end())
        return detail_response

    async def scenario() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = RemoteRunClient("http://runtime.test", client=http)
        handle = await client.start(_request())

        with pytest.raises(RemoteRunClientError, match=error):
            await handle.wait()

        await client.close()
        await http.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={}),
        httpx.Response(200, json={"command": {"kind": "stop"}}),
    ],
)
def test_remote_client_rejects_invalid_control_data(response: httpx.Response) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return response

    async def scenario() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = RemoteRunClient("http://runtime.test", client=http)

        with pytest.raises(RemoteRunClientError, match="invalid control data"):
            await client.stop("run_remote")

        await client.close()
        await http.aclose()

    asyncio.run(scenario())


def test_remote_client_close_detaches_active_stream_and_closes_owned_http() -> None:
    release = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return _stream_response(
            _begin(),
            stream=_Bytes(
                (
                    "event: run_begin\n"
                    f"data: {json.dumps(run_event_to_data(_begin()))}\n\n"
                ).encode(),
                wait=release,
            ),
        )

    async def scenario() -> None:
        transport = _Transport(handler)
        http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        client = RemoteRunClient("http://runtime.test", client=http)

        handle = await client.start(_request())
        await asyncio.sleep(0)
        await client.close()

        with pytest.raises(RemoteRunClientError, match="client is closed"):
            await handle.wait()
        with pytest.raises(RemoteRunClientError, match="client is closed"):
            await client.start(_request())
        assert not http.is_closed
        await http.aclose()

        owned = RemoteRunClient("http://runtime.test")
        owned_http = owned._http
        await owned.close()
        assert owned_http.is_closed

    asyncio.run(scenario())


def test_remote_client_isolates_tracer_failures() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return _stream_response(_begin(), _end())
        return httpx.Response(
            200,
            json=_DETAIL_ADAPTER.dump_python(_detail(), mode="json"),
        )

    async def scenario() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = RemoteRunClient("http://runtime.test", client=http)
        tracer = _Tracer(fail=True)

        handle = await client.start(_request(), tracer=tracer)
        detail = await handle.wait()

        assert detail.status == "succeeded"
        assert [event.type for event in tracer.events] == ["run_begin", "run_end"]
        await client.close()
        await http.aclose()

    asyncio.run(scenario())
