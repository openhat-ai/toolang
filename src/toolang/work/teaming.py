"""One lease owns agent messaging and optional observation export."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging

from toolang.teaming.event_backend import EventBackend
from toolang.teaming.exporter import EventExporter
from toolang.teaming.messaging import RENEW_SECONDS
from .messaging import MessagingLoop

logger = logging.getLogger(__name__)


class TeamingLoop:
    def __init__(self, messaging: MessagingLoop) -> None:
        self.messaging = messaging
        self.exporter = EventExporter(
            messaging.executor.stream,
            messaging.executor.store.db_path,
            EventBackend(messaging.client._backend),
            agent=messaging.agent,
            token=messaging.client.token,
        )
        self._stop = asyncio.Event()
        self._messages = asyncio.Event()
        self._consumer: asyncio.Task[None] | None = None
        self._export: asyncio.Task[None] | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _consume(self) -> None:
        try:
            self.messaging.load()
        except Exception:
            logger.exception("Messaging checkpoint is invalid")
            return
        while not self._messages.is_set():
            try:
                await self.messaging.consume(self._messages)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Messaging disconnected; retrying")
                await self.messaging._wait(self._messages, 1)

    async def _export_events(self) -> None:
        while not self._stop.is_set():
            try:
                await self.exporter.run()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Event exporter failed; recovering independently")
                await self.messaging._wait(self._stop, 1)

    async def _heartbeat(self) -> None:
        while not self._stop.is_set():
            await self.messaging._wait(self._stop, RENEW_SECONDS)
            if not self._stop.is_set():
                await self.messaging.client.renew()

    async def _run(self) -> None:
        delay = 0.5
        while not self._stop.is_set():
            try:
                await self.messaging.client.register(
                    self.messaging.owner, endpoint=self.messaging.endpoint
                )
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(self._heartbeat())
                    self._consumer = tasks.create_task(self._consume())
                    self._export = tasks.create_task(self._export_events())
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Teaming lease unavailable; reconnecting")
                await self.messaging._wait(self._stop, delay)
                delay = min(5, delay * 2)

    async def stop_messages(self) -> None:
        self._messages.set()
        if self._consumer is not None:
            self._consumer.cancel()
            with suppress(asyncio.CancelledError):
                await self._consumer

    async def close(self) -> None:
        # Called after executor.stop() persisted its final events, while the
        # heartbeat and lease still belong to this lifecycle.
        self.exporter.finish()
        try:
            if self._export is not None:
                with suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(self._export), 5)
        finally:
            self._stop.set()
            if self._task is not None:
                self._task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._task
            self.exporter.close()
            with suppress(Exception):
                await self.messaging.client.unregister()
            await self.messaging.client.close()
