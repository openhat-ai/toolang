"""Streaming response helpers for agent HTTP components."""

from __future__ import annotations

import asyncio
import threading

from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

DISCONNECT_POLL_SEC = 0.1


class ShutdownAwareStreamingResponse(StreamingResponse):
    """One streaming response that exits promptly after runtime shutdown starts."""

    def __init__(
        self,
        *args,
        shutdown_signal: threading.Event | None = None,
        disconnect_poll_sec: float = DISCONNECT_POLL_SEC,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._shutdown_signal = shutdown_signal
        self._disconnect_poll_sec = disconnect_poll_sec

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except asyncio.CancelledError:
            if self._shutdown_signal is not None and self._shutdown_signal.is_set():
                return
            raise

    async def listen_for_disconnect(self, receive: Receive) -> None:
        if self._shutdown_signal is None:
            await super().listen_for_disconnect(receive)
            return
        while True:
            if self._shutdown_signal.is_set():
                return
            try:
                message = await asyncio.wait_for(receive(), timeout=self._disconnect_poll_sec)
            except asyncio.TimeoutError:
                continue
            if message["type"] == "http.disconnect":
                return
