"""Coalesced refresh notifications; reconnects always start from current state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
from typing import Self

import httpx
from httpx_sse import SSEError, aconnect_sse


class Changes:
    def __init__(
        self,
        http: httpx.AsyncClient,
        path: str,
        *,
        checkpoint: str,
        matches: Callable[[str, dict], bool],
        headers: dict[str, str] | None = None,
    ) -> None:
        self.http, self.path = http, path
        self.checkpoint, self.matches, self.headers = checkpoint, matches, headers
        self.connected = False
        self.changed = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def __aenter__(self) -> Self:
        self.task = asyncio.create_task(self._listen())
        self.task.add_done_callback(lambda _: self.changed.set())
        return self

    async def __aexit__(self, *args: object) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def wait(self, timeout: float | None = None) -> None:
        try:
            await asyncio.wait_for(self.changed.wait(), timeout)
        except TimeoutError:
            pass
        self.changed.clear()
        if self.task is not None and self.task.done():
            await self.task

    async def _listen(self) -> None:
        delay = 0.5
        while True:
            try:
                async with aconnect_sse(
                    self.http,
                    "GET",
                    self.path,
                    headers=self.headers or {},
                    timeout=httpx.Timeout(10, read=30),
                ) as stream:
                    stream.response.raise_for_status()
                    async for event in stream.aiter_sse():
                        if event.event in {"stream_error", "resync_required"}:
                            raise httpx.ReadError("Subscription needs a fresh snapshot")
                        if not self.connected:
                            if event.event != self.checkpoint:
                                continue
                            self.connected = True
                            delay = 0.5
                            self.changed.set()
                        elif event.data and event.event != self.checkpoint:
                            data = json.loads(event.data)
                            if not isinstance(data, dict):
                                raise ValueError("Invalid change notification")
                            if self.matches(event.event, data):
                                self.changed.set()
                    raise httpx.ReadError("Subscription disconnected")
            except (httpx.HTTPError, SSEError, ValueError):
                self.connected = False
                self.changed.set()
                await asyncio.sleep(delay)
                delay = min(5, delay * 2)
