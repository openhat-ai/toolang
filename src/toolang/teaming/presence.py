"""Bounded, cancellable expiry reconciliation owned by a Hub lifespan."""

import asyncio
from collections.abc import Awaitable, Callable
import logging

from .backend import Backend
from .errors import BackendUnavailable, MessagingError
from .types import STORAGE_BATCH_SIZE

logger = logging.getLogger(__name__)


class PresenceWorker:
    def __init__(
        self,
        backend: Backend,
        *,
        wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.backend, self.wait = backend, wait

    async def reconcile_once(self) -> int:
        count = 0
        for root in await self.backend.roots():
            scoped = self.backend.for_root(root)
            try:
                due = await scoped.due_presence()
                for agent in due:
                    await scoped.expire_presence(agent)
                count += len(due)
            except BackendUnavailable:
                raise
            except MessagingError:
                logger.exception("Could not reconcile presence in root %s", root)
        return count

    async def run(self) -> None:
        delay = 1.0
        while True:
            try:
                count = await self.reconcile_once()
            except BackendUnavailable:
                logger.warning("Presence backend unavailable; retrying reconciliation")
                await self.wait(delay)
                delay = min(delay * 2, 5)
            else:
                delay = 1.0
                await self.wait(0 if count >= STORAGE_BATCH_SIZE else 1)
