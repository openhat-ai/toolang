"""Runtime transport with an optional incoming-message driver."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging

from toolang.execution.executor import RunExecutor
from toolang.teaming.types import AgentTransport

from toolang.teaming.exporter import EventExporter
from toolang.teaming.types import EventPublisher, RENEW_SECONDS
from toolang.driver.talk import MessagingLoop

logger = logging.getLogger(__name__)


class TeamingLoop:
    def __init__(
        self,
        publisher: EventPublisher,
        *,
        client: AgentTransport,
        owner: str,
        endpoint: str,
        executor: RunExecutor,
        messaging: MessagingLoop | None = None,
    ) -> None:
        self.messaging = messaging
        self.client = client
        self.owner = owner
        self.endpoint = endpoint
        self.exporter = EventExporter(
            executor.stream,
            executor.store.db_path,
            publisher,
            agent=client.actor,
            token=client.token,
        )
        self._stop = asyncio.Event()
        self._messages = asyncio.Event()
        self._consumer: asyncio.Task[None] | None = None
        self._export: asyncio.Task[None] | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _consume(self) -> None:
        assert self.messaging is not None
        while not self._messages.is_set():
            try:
                await self.messaging.consume(self._messages)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Messaging disconnected; retrying")
                await _wait(self._messages, 1)

    async def _export_events(self) -> None:
        while not self._stop.is_set():
            try:
                await self.exporter.run()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Event exporter failed; recovering independently")
                await _wait(self._stop, 1)

    async def _run(self) -> None:
        delay = 0.5
        registered = False
        async with asyncio.TaskGroup() as tasks:
            while not self._stop.is_set():
                try:
                    if registered:
                        await self.client.renew()
                    else:
                        await self.client.register(self.owner, endpoint=self.endpoint)
                        registered = True
                        if self._export is None:
                            if self.messaging is not None:
                                self._consumer = tasks.create_task(self._consume())
                            self._export = tasks.create_task(self._export_events())
                    delay = 0.5
                    await _wait(self._stop, RENEW_SECONDS)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Communication failures never cancel an in-flight handler.
                    registered = False
                    logger.warning("Teaming Hub unavailable; reconnecting")
                    await _wait(self._stop, delay)
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
                # Lease expiry handles an unavailable backend. Do not add its
                # full socket timeout after the final-event drain budget.
                async with asyncio.timeout(1):
                    await self.client.unregister()
            await self.client.close()


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except TimeoutError:
        pass
