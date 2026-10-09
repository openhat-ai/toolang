"""One lease owns agent messaging and optional observation export."""

from __future__ import annotations

import asyncio
from contextlib import aclosing, suppress
from collections.abc import Awaitable, Callable
from toolang.execution.schemas import ActivitySnapshot
import logging

from toolang.execution.activity import ActivityQuery, ActivityReader

from toolang.teaming.exporter import EventExporter
from toolang.teaming.types import EventPublisher, RENEW_SECONDS
from .messaging import MessagingLoop

logger = logging.getLogger(__name__)


class TeamingLoop:
    def __init__(
        self,
        messaging: MessagingLoop,
        publisher: EventPublisher,
        *,
        activity: ActivityReader | None = None,
        publish_activity: Callable[[list[ActivitySnapshot]], Awaitable[None]]
        | None = None,
    ) -> None:
        self.messaging = messaging
        self.exporter = EventExporter(
            messaging.executor.stream,
            messaging.executor.store.db_path,
            publisher,
            agent=messaging.agent,
            token=messaging.client.token,
        )
        self.activity = activity
        self.publish_activity = publish_activity
        self._activity: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._messages = asyncio.Event()
        self._consumer: asyncio.Task[None] | None = None
        self._export: asyncio.Task[None] | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _consume(self) -> None:
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

    async def _publish_activity(self) -> None:
        assert self.activity is not None and self.publish_activity is not None
        while not self._stop.is_set():
            try:
                async with aclosing(self.activity.updates(ActivityQuery())) as updates:
                    async for pages in updates:
                        if self._stop.is_set():
                            return
                        await self.publish_activity(pages)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Activity publication unavailable", exc_info=True)
                await self.messaging._wait(self._stop, 1)

    async def _run(self) -> None:
        delay = 0.5
        registered = False
        async with asyncio.TaskGroup() as tasks:
            while not self._stop.is_set():
                try:
                    if registered:
                        await self.messaging.client.renew()
                    else:
                        await self.messaging.client.register(
                            self.messaging.owner, endpoint=self.messaging.endpoint
                        )
                        registered = True
                        if self._consumer is None:
                            self._consumer = tasks.create_task(self._consume())
                            self._export = tasks.create_task(self._export_events())
                            if (
                                self.activity is not None
                                and self.publish_activity is not None
                            ):
                                self._activity = tasks.create_task(
                                    self._publish_activity()
                                )
                    delay = 0.5
                    await self.messaging._wait(self._stop, RENEW_SECONDS)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Communication failures never cancel an in-flight handler.
                    registered = False
                    logger.warning("Teaming Hub unavailable; reconnecting")
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
        if self.activity is not None and self.publish_activity is not None:
            with suppress(Exception):
                async with asyncio.timeout(2):
                    pages = await asyncio.to_thread(
                        self.activity.pages, ActivityQuery()
                    )
                    await self.publish_activity(pages)
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
                # Lease expiry handles an unavailable backend. Do not add its
                # full socket timeout after the final-event drain budget.
                async with asyncio.timeout(1):
                    await self.messaging.client.unregister()
            await self.messaging.client.close()
