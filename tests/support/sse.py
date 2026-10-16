"""Deterministic, controllable SSE response bytes."""

import asyncio
import json

import httpx


class Events(httpx.AsyncByteStream):
    def __init__(self, checkpoint):
        self.checkpoint = checkpoint
        self.queue = asyncio.Queue()
        self.closed = False
        self.handled = None

    async def __aiter__(self):
        yield f"event: {self.checkpoint}\ndata: {{}}\n\n".encode()
        while True:
            item, handled = await self.queue.get()
            self.handled = handled
            if item is None:
                handled.set()
                return
            yield item
            handled.set()

    async def send(self, event, data=None):
        handled = asyncio.Event()
        await self.queue.put(
            (
                f"event: {event}\ndata: {json.dumps(data or {})}\n\n".encode()
                if event is not None
                else None,
                handled,
            )
        )
        await asyncio.wait_for(handled.wait(), 2)

    async def aclose(self):
        self.closed = True
        if self.handled:
            self.handled.set()
