"""Subscription dependencies, SSE encoding, and per-write transport deadlines."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Query, Request
from fastapi.sse import ServerSentEvent
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from toolang.execution.errors import StreamOverflowError
from toolang.execution.subscriptions import (
    Attachment,
    EventSubscription,
    SnapshotLimitError,
    Subscriptions,
)

RUN_ID_HEADER = "X-Toolang-Run-ID"
KEEP_ALIVE_SEC = 15.0
SEND_TIMEOUT_SEC = 5.0


async def reserve_stream(
    request: Request,
    after: Annotated[str | None, Query()] = None,
) -> AsyncIterator[Attachment]:
    subscriptions = cast(Subscriptions, request.app.state.subscriptions)
    try:
        attachment = await subscriptions.reserve(after)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        yield attachment
    finally:
        attachment.close()


StreamAttachmentDep = Annotated[Attachment, Depends(reserve_stream)]


async def sse_stream(
    request: Request,
    subscription: EventSubscription,
) -> AsyncGenerator[ServerSentEvent, None]:
    """Transport controls are private to this connection, never source events."""
    try:
        while True:
            signal = getattr(request.app.state, "shutdown_signal", None)
            if signal is not None and signal.is_set():
                return
            try:
                frame = await asyncio.wait_for(subscription.receive(), KEEP_ALIVE_SEC)
            except TimeoutError:
                frame = subscription.checkpoint()
                if frame is None:
                    yield ServerSentEvent(comment="keep-alive")
                    continue
            yield ServerSentEvent(event=frame.event, data=frame.data, id=frame.id)
    except (StreamOverflowError, SnapshotLimitError) as exc:
        code = "overflow" if isinstance(exc, StreamOverflowError) else "snapshot_limit"
        yield ServerSentEvent(event="stream_error", data={"code": code})
    except StopAsyncIteration:
        checkpoint = subscription.checkpoint()
        if checkpoint is not None:
            yield ServerSentEvent(
                event=checkpoint.event, data=checkpoint.data, id=checkpoint.id
            )
    finally:
        subscription.close()


class SSESendDeadline:
    """Bound each SSE ASGI send, including headers; idle reads have no deadline."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        streaming = False

        async def bounded_send(message: Message) -> None:
            nonlocal streaming
            if message["type"] == "http.response.start":
                streaming = any(
                    name.lower() == b"content-type"
                    and value.startswith(b"text/event-stream")
                    for name, value in message.get("headers", ())
                )
            if streaming:
                async with asyncio.timeout(SEND_TIMEOUT_SEC):
                    await send(message)
            else:
                await send(message)

        await self.app(scope, receive, bounded_send)
