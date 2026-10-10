"""Root-owned roster reconciliation, independent of retained history."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging

from .backend import Backend
from .errors import MessagingError, StorageIntegrityError

logger = logging.getLogger(__name__)


class Roster:
    def __init__(
        self,
        backend: Backend,
        *,
        root: str,
        owner: str,
        discover: Callable[[], set[str]],
    ) -> None:
        self.backend, self.root, self.owner, self.discover = (
            backend,
            root,
            owner,
            discover,
        )
        self._lock = asyncio.Lock()

    async def agents(self) -> dict[str, dict]:
        records = await self.backend.roster()
        return {
            agent: row for agent, row in records.items() if row["root"] == self.root
        }

    async def _replace(
        self, agent: str, previous: dict | None, updated: dict | None
    ) -> bool:
        return await self.backend.reconcile_roster(agent, self.owner, previous, updated)

    async def scan(self) -> None:
        async with self._lock:
            found = await asyncio.to_thread(self.discover)
            entries = await self.agents()
            for agent, old in entries.items():
                if not old["managed"]:
                    continue
                missing = 0 if agent in found else old["missing"] + 1
                if missing >= 2 and not await self.backend.online(agent):
                    await self._replace(agent, old, None)
                elif missing != old["missing"]:
                    await self._replace(agent, old, {**old, "missing": missing})
            for agent in found - entries.keys():
                # HGET/CAS refuses to adopt another root's managed entries.
                await self._replace(
                    agent, None, {"root": self.root, "managed": True, "missing": 0}
                )

    async def register(
        self, agent: str, token: str, endpoint: str, managed: bool
    ) -> None:
        if managed:
            await self.scan()
            record = (await self.agents()).get(agent)
            if not record or not record["managed"] or record["missing"]:
                raise MessagingError("Agent home is unavailable")
        else:
            async with self._lock:
                record = (await self.agents()).get(agent)
                if record and record["managed"]:
                    raise MessagingError("Agent name belongs to a resident home")
                if record is None and not await self._replace(
                    agent, None, {"root": self.root, "managed": False, "missing": 0}
                ):
                    raise MessagingError("Agent name belongs to another root")
        await self.backend.register(
            self.owner, agent=agent, token=token, endpoint=endpoint, root=self.root
        )

    async def run(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self.scan()
            except StorageIntegrityError:
                raise
            except Exception:
                logger.warning(
                    "Agent roster scan unavailable; retaining previous state",
                    exc_info=True,
                )
