"""Per-write deadlines shared by local agent and Hub SSE transports."""

import asyncio
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SEND_TIMEOUT_SEC = 5.0


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
