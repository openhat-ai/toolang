"""Bounded, cancellable expiry reconciliation owned by a Hub lifespan."""

import asyncio
from collections.abc import Awaitable, Callable
import logging

from .backend import Backend
from .errors import BackendUnavailable
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
        due = await self.backend.due_presence()
        for agent in due:
            await self.backend.expire_presence(agent)
        return len(due)

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
                await self.wait(0 if count == STORAGE_BATCH_SIZE else 1)
