"""Bound real ASGI writes without imposing deadlines on idle subscriptions."""

import asyncio

import pytest

from toolang.common import sse as common


@pytest.mark.parametrize("blocked", ["http.response.start", "http.response.body"])
def test_sse_send_deadline_covers_headers_and_body(monkeypatch, blocked):
    monkeypatch.setattr(common, "SEND_TIMEOUT_SEC", 0.01)
    cleaned = False

    async def app(scope, receive, send):
        nonlocal cleaned
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                }
            )
            await send({"type": "http.response.body", "body": b"data: hello\n\n"})
        finally:
            cleaned = True

    async def receive():
        await asyncio.Future()

    async def send(message):
        if message["type"] == blocked:
            await asyncio.Future()

    async def scenario():
        with pytest.raises(TimeoutError):
            await common.SSESendDeadline(app)({"type": "http"}, receive, send)
        assert cleaned

    asyncio.run(scenario())


def test_idle_stream_wait_does_not_consume_send_deadline(monkeypatch):
    monkeypatch.setattr(common, "SEND_TIMEOUT_SEC", 0.001)
    messages = []

    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await asyncio.sleep(0.02)
        await send({"type": "http.response.body", "body": b": keep-alive\n\n"})

    async def receive():
        await asyncio.Future()

    async def send(message):
        messages.append(message)

    asyncio.run(common.SSESendDeadline(app)({"type": "http"}, receive, send))
    assert len(messages) == 2


def test_native_sse_header_timeout_releases_fastapi_dependency(monkeypatch):
    from collections.abc import AsyncIterator
    from typing import Annotated

    from fastapi import Depends, FastAPI
    from fastapi.sse import EventSourceResponse, ServerSentEvent
    from starlette.requests import ClientDisconnect

    monkeypatch.setattr(common, "SEND_TIMEOUT_SEC", 0.01)
    released = False

    async def dependency():
        nonlocal released
        try:
            yield "attached"
        finally:
            released = True

    app = FastAPI()

    @app.get("/stream", response_class=EventSourceResponse)
    async def stream(
        value: Annotated[str, Depends(dependency)],
    ) -> AsyncIterator[ServerSentEvent]:
        yield ServerSentEvent(data=value)

    async def receive():
        await asyncio.Future()

    async def send(message):
        await asyncio.Future()

    async def scenario():
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/stream",
            "raw_path": b"/stream",
            "query_string": b"",
            "headers": [],
        }
        with pytest.raises(ExceptionGroup) as raised:
            await common.SSESendDeadline(app)(scope, receive, send)
        assert raised.value.subgroup(ClientDisconnect) is not None
        assert released

    asyncio.run(scenario())


def test_native_sse_ping_replaces_manual_idle_keepalive(monkeypatch):
    from collections.abc import AsyncIterator
    from typing import cast

    from fastapi import FastAPI, Request
    from fastapi.sse import EventSourceResponse, ServerSentEvent
    import fastapi.routing

    from toolang.api import common as agent_sse
    from toolang.execution.schemas import StreamFrame
    from toolang.execution.subscriptions import EventSubscription

    monkeypatch.setattr(fastapi.routing, "_PING_INTERVAL", 0.01)
    monkeypatch.setattr(agent_sse, "CHECKPOINT_INTERVAL_SEC", 0.005)
    app = FastAPI()
    closed = False

    class Subscription:
        first = True

        async def receive(self):
            if self.first:
                self.first = False
                return StreamFrame("run_begin", {"type": "run_begin"}, "cursor")
            await asyncio.Event().wait()

        def checkpoint(self):
            return None

        def close(self):
            nonlocal closed
            closed = True

    subscription = Subscription()

    @app.get("/stream", response_class=EventSourceResponse)
    async def stream(request: Request) -> AsyncIterator[ServerSentEvent]:
        async for event in agent_sse.sse_stream(
            request, cast(EventSubscription, subscription)
        ):
            yield event

    async def scenario():
        chunks: list[bytes] = []
        ping = asyncio.Event()

        async def send(message):
            body = message.get("body", b"")
            chunks.append(body)
            if b": ping\n\n" in body:
                ping.set()

        async def receive():
            await asyncio.Event().wait()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/stream",
            "raw_path": b"/stream",
            "query_string": b"",
            "headers": [],
        }
        task = asyncio.create_task(app(scope, receive, send))
        try:
            await asyncio.wait_for(ping.wait(), timeout=1)
            await asyncio.sleep(0.03)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert b"event: run_begin" in b"".join(chunks)
        assert b": ping\n\n" in b"".join(chunks)
        assert b": keep-alive\n\n" not in b"".join(chunks)
        assert closed

    asyncio.run(scenario())
