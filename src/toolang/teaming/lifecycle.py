"""Ownership of the Hub's shared backend and maintenance tasks."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
import logging

from .errors import StorageIntegrityError
from .messaging import MessagingClient
from .presence import PresenceWorker
from .roster import Roster

logger = logging.getLogger(__name__)


class HubLifecycle:
    def __init__(self, client: MessagingClient, *, roster: Roster | None = None):
        self.client, self.roster = client, roster
        self.presence = PresenceWorker(client._backend)
        self.tasks: list[asyncio.Task] = []
        self.ready = False
        self.failed = False
        self.stopping = False

    def _observe(self, task: asyncio.Task) -> None:
        if self.stopping:
            return
        self.failed = True
        self.ready = False
        if not task.cancelled():
            error = task.exception()
            logger.error(
                "Hub maintenance worker stopped: %s", error or "unexpected return"
            )

    @asynccontextmanager
    async def lifespan(
        self, on_ready: Callable[[], None] | None = None
    ) -> AsyncIterator[None]:
        self.stopping = self.failed = self.ready = False
        self.tasks = []
        try:
            await self.client.check_backend()
            await self.client.register_human()
            await self.presence.reconcile_once()
            if self.roster:
                try:
                    await self.roster.scan()
                except StorageIntegrityError:
                    raise
                except Exception:
                    logger.warning(
                        "Initial roster scan unavailable; retaining saved entries",
                        exc_info=True,
                    )
            self.tasks.append(
                asyncio.create_task(self.presence.run(), name="hub-presence")
            )
            if self.roster:
                self.tasks.append(
                    asyncio.create_task(self.roster.run(), name="hub-roster")
                )
            for task in self.tasks:
                task.add_done_callback(self._observe)
            await asyncio.sleep(0)
            if any(task.done() for task in self.tasks):
                raise RuntimeError("Hub maintenance worker failed during startup")
            self.ready = True
            if on_ready:
                on_ready()
            yield
        finally:
            self.ready = False
            self.stopping = True
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await self.client.close()
