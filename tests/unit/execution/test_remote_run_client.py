"""HTTP run client transport, protocol, and lifecycle behavior."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
import json

import httpx
import pytest
from pydantic import TypeAdapter

from toolang.base.types.message import Message
from toolang.base.types.model import (
    ModelParameters,
    ModelRequest,
    ReasoningParameters,
)
from toolang.base.types.policy import AgentCeiling, RunLimits, RunPolicy
from toolang.execution.events import (
    RunBegin,
    RunEnd,
    RunEvent,
    RunTracer,
    run_event_to_data,
)
from toolang.execution.records import SteerControlPayload, CancelControlPayload
from toolang.execution.remote import RemoteRunClient, RemoteRunClientError
from toolang.execution.schemas import (
    ControlInfo,
    RerunRequest,
    RetryRequest,
    RunControlRefData,
    RunDetail,
    RunRequest,
    RunnableRequest,
)
from toolang.execution.types import ControlRef, Local, RunCommand, StepPath
from toolang.lang.input import NamedInputSource, RunnableInputRaw


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
        thread_id="term_test",
        request_id="term_request",
        runnable=RunnableRequest(
            "agic:chat",
            RunnableInputRaw(
                _="hello",
                named=(NamedInputSource("tone", "brief"),),
            ),
        ),
        model=ModelRequest("openai/gpt-5"),
        policy=RunPolicy(
            allow=(AgentCeiling(models=("openai/*",)),),
            limits=RunLimits(cost=Decimal("2.50")),
        ),
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
        state=RunControlRefData(run=run_id, index=0),
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
        payload = CancelControlPayload((Local.typed("Text", "finished", "_", 0),))
        kind = "cancel"
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


def test_remote_client_runs_traces_and_waits_for_detail() -> None:
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

        await client.connect()
        handle = await client.run(_request(), tracer=tracer)

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
                "thread_id": "term_test",
                "request_id": "term_request",
                "runnable": {
                    "ref": "agic:chat",
                    "input": {
                        "_": "hello",
                        "named": [{"name": "tone", "source": "brief"}],
                    },
                },
                "model": {
                    "ref": "openai/gpt-5",
                    "parameters": {"reasoning": None},
                },
                "policy": {
                    "allow": [
                        {
                            "models": ["openai/*"],
                            "tools": None,
                            "psyches": None,
                            "skills": None,
                            "services": None,
                            "prompts": None,
                        }
                    ],
                    "limits": {
                        "agic_model_calls": 200,
                        "agic_tool_calls": None,
                        "tokens": None,
                        "cost": "2.50",
                        "time": None,
                    },
                },
            },
        )

        await client.disconnect()
        await client.disconnect()
        assert not http.is_closed
        await http.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ("retry", "rerun"))
def test_remote_client_reuses_the_run_stream_protocol_for_restarts(
    operation: str,
) -> None:
    accepted_id = "run_source" if operation == "retry" else "run_rerun"
    detail_reads = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal detail_reads
        if request.method == "POST":
            return _stream_response(
                _begin(accepted_id),
                _end(accepted_id),
                run_id=accepted_id,
            )
        detail_reads += 1
        return httpx.Response(
            200,
            json=_DETAIL_ADAPTER.dump_python(_detail(accepted_id), mode="json"),
        )

    async def scenario() -> None:
        transport = _Transport(handler)
        http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        client = RemoteRunClient("http://runtime.test", client=http)
        tracer = _Tracer()
        request = (
            RetryRequest(
                source="run_source",
                commands=(RunCommand("limit", "cost", Decimal("2.50")),),
                request_id="retry_request",
                anchor=StepPath("run_source", (1, 2)),
            )
            if operation == "retry"
            else RerunRequest(
                source="run_source",
                commands=(RunCommand("limit", "time", 30),),
                request_id="rerun_request",
                model=ModelRequest(
                    "openai/gpt-5",
                    ModelParameters(ReasoningParameters("high")),
                ),
            )
        )

        await client.connect()
        handle = (
            await client.retry(request, tracer=tracer)
            if isinstance(request, RetryRequest)
            else await client.rerun(request, tracer=tracer)
        )
        assert tracer.events == []
        detail = await handle.wait()
        assert await handle.wait() == detail

        expected_payload: dict[str, object] = {
            "request_id": f"{operation}_request",
            "commands": [
                {
                    "group": "limit",
                    "field": "cost" if operation == "retry" else "time",
                    "value": "2.50" if operation == "retry" else 30,
                }
            ],
        }
        if operation == "retry":
            expected_payload["anchor"] = "run_source.1.2"
        else:
            expected_payload["model"] = {
                "ref": "openai/gpt-5",
                "parameters": {"reasoning": {"effort": "high"}},
            }
        assert handle.run_id == detail.id == accepted_id
        assert [event.type for event in tracer.events] == ["run_begin", "run_end"]
        assert detail_reads == 2
        assert transport.requests[0] == (
            "POST",
            f"http://runtime.test/api/v1/runs/run_source/{operation}/stream",
            expected_payload,
        )

        await client.disconnect()
        await http.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("operation", "accepted_id", "error"),
    [
        ("retry", "run_other", "did not accept the source run ID"),
        ("rerun", "run_source", "did not accept a new run ID"),
    ],
)
def test_remote_client_rejects_invalid_restart_identity(
    operation: str,
    accepted_id: str,
    error: str,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return _stream_response(run_id=accepted_id)

    async def scenario() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = RemoteRunClient("http://runtime.test", client=http)
        await client.connect()

        with pytest.raises(RemoteRunClientError, match=error):
            if operation == "retry":
                await client.retry(
                    RetryRequest(
                        source="run_source",
                        commands=(),
                        request_id="retry_request",
                    )
                )
            else:
                await client.rerun(
                    RerunRequest(
                        source="run_source",
                        commands=(),
                        request_id="rerun_request",
                    )
                )

        await client.disconnect()
        await http.aclose()

    asyncio.run(scenario())


def test_remote_client_maps_cancel_and_empty_steer_controls() -> None:
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

        await client.connect()
        cancellation = await client.cancel(
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

        assert cancellation == _control("cancel")
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

        await client.disconnect()
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
        "http://runtime.test\x00",
        "http://runtime.test\\evil",
        "http://runtime.test|evil",
        "http://runtime.test%zz",
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

        await client.connect()
        if "non-SSE" in error:
            with pytest.raises(RemoteRunClientError, match=error):
                await client.run(_request())
        else:
            handle = await client.run(_request())
            with pytest.raises(RemoteRunClientError, match=error):
                await handle.wait()

        await client.disconnect()
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

        await client.connect()
        with pytest.raises(RemoteRunClientError) as caught:
            await client.run(_request())

        assert str(caught.value) == ("remote run transport failed: ConnectError")
        assert "runtime.test" not in str(caught.value)
        assert requests == 1
        await client.disconnect()
        await http.aclose()

    asyncio.run(scenario())


def test_remote_client_settles_unexpected_reader_failure() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("HTTP client failed unexpectedly")

    async def scenario() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = RemoteRunClient("https://runtime.test", client=http)

        await client.connect()
        with pytest.raises(
            RemoteRunClientError,
            match="remote run failed: RuntimeError",
        ):
            await asyncio.wait_for(client.run(_request()), timeout=1)

        await asyncio.sleep(0)
        assert not client._readers
        await client.disconnect()
        await http.aclose()

    asyncio.run(scenario())


def test_remote_client_rejects_closed_injected_http_client() -> None:
    async def scenario() -> None:
        http = httpx.AsyncClient()
        await http.aclose()
        client = RemoteRunClient("https://runtime.test", client=http)

        with pytest.raises(RemoteRunClientError, match="HTTP client is closed"):
            await client.connect()

        await client.disconnect()

    asyncio.run(scenario())


def test_remote_client_preserves_http_error_detail() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "Runnable not found: chat"})

    async def scenario() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = RemoteRunClient("http://runtime.test", client=http)

        await client.connect()
        with pytest.raises(RemoteRunClientError) as caught:
            await client.run(_request())

        assert caught.value.status_code == 422
        assert caught.value.detail == "Runnable not found: chat"
        assert str(caught.value) == (
            "remote run failed: HTTP 422 Runnable not found: chat"
        )
        await client.disconnect()
        await http.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("detail_response", "error"),
    [
        (httpx.Response(200, content=b"{"), "wait returned invalid JSON"),
        (httpx.Response(200, json={"id": "run_remote"}), "invalid run detail"),
        (
            httpx.Response(
                200,
                json=_DETAIL_ADAPTER.dump_python(
                    _detail("run_other"),
                    mode="json",
                ),
            ),
            "invalid run detail",
        ),
        (
            httpx.Response(
                200,
                json=_DETAIL_ADAPTER.dump_python(
                    replace(_detail(), root_run_id="run_other"),
                    mode="json",
                ),
            ),
            "invalid run detail",
        ),
        (
            httpx.Response(
                200,
                json=_DETAIL_ADAPTER.dump_python(
                    replace(_detail(), parent=StepPath("run_parent", (0,))),
                    mode="json",
                ),
            ),
            "invalid run detail",
        ),
        (
            httpx.Response(
                200,
                json=_DETAIL_ADAPTER.dump_python(
                    replace(_detail(), status="running", finished_at=None),
                    mode="json",
                ),
            ),
            "invalid run detail",
        ),
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
        await client.connect()
        handle = await client.run(_request())

        with pytest.raises(RemoteRunClientError, match=error):
            await handle.wait()

        await client.disconnect()
        await http.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={}),
        httpx.Response(200, json={"command": {"kind": "stop"}}),
        httpx.Response(
            200,
            json={
                "command": _CONTROL_ADAPTER.dump_python(
                    _control("cancel", "run_other"),
                    mode="json",
                )
            },
        ),
        httpx.Response(
            200,
            json={
                "command": _CONTROL_ADAPTER.dump_python(
                    _control("steer"),
                    mode="json",
                )
            },
        ),
    ],
)
def test_remote_client_rejects_invalid_control_data(response: httpx.Response) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return response

    async def scenario() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = RemoteRunClient("http://runtime.test", client=http)

        await client.connect()
        with pytest.raises(RemoteRunClientError, match="invalid control data"):
            await client.cancel("run_remote")

        await client.disconnect()
        await http.aclose()

    asyncio.run(scenario())


def test_remote_client_disconnect_detaches_stream_without_canceling_remote_run() -> (
    None
):
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

        await client.connect()
        handle = await client.run(_request())
        await asyncio.sleep(0)
        await client.disconnect()

        with pytest.raises(RemoteRunClientError, match="client is disconnected"):
            await handle.wait()
        with pytest.raises(RemoteRunClientError, match="client is disconnected"):
            await client.run(_request())
        assert [(method, url) for method, url, _payload in transport.requests] == [
            ("POST", "http://runtime.test/api/v1/runs/authored/stream")
        ]
        assert not http.is_closed
        await http.aclose()

        owned = RemoteRunClient("http://runtime.test")
        await owned.connect()
        owned_http = owned._http
        await owned.disconnect()
        assert owned_http is not None
        assert owned_http.is_closed

    asyncio.run(scenario())


def test_remote_client_disconnect_settles_pre_acceptance_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()

    async def wait_without_acceptance(*_args: object, **_kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    async def scenario() -> None:
        http = httpx.AsyncClient()
        client = RemoteRunClient("http://runtime.test", client=http)
        monkeypatch.setattr(client, "_read_run_stream", wait_without_acceptance)

        await client.connect()
        pending = asyncio.create_task(client.run(_request()))
        await asyncio.wait_for(entered.wait(), timeout=1)
        await client.disconnect()

        with pytest.raises(RemoteRunClientError, match="client is disconnected"):
            await asyncio.wait_for(pending, timeout=1)

        await http.aclose()

    asyncio.run(scenario())


def test_remote_client_reconnects_with_a_fresh_owned_http_client() -> None:
    async def scenario() -> None:
        client = RemoteRunClient("http://runtime.test")
        assert not client.connected
        assert client._http is None
        with pytest.raises(RemoteRunClientError, match="client is disconnected"):
            await client.run(_request())
        await client.connect()
        original = client._http
        await client.connect()
        assert client._http is original

        await client.disconnect()
        assert original is not None
        assert original.is_closed

        await client.connect()
        assert client.connected
        assert client._http is not original
        assert client._http is not None
        assert not client._http.is_closed

        await client.disconnect()

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

        await client.connect()
        handle = await client.run(_request(), tracer=tracer)
        detail = await handle.wait()

        assert detail.status == "succeeded"
        assert [event.type for event in tracer.events] == ["run_begin", "run_end"]
        await client.disconnect()
        await http.aclose()

    asyncio.run(scenario())
