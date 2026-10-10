"""Hub activity federation from local stores, agent HTTP and cached coverage."""

from __future__ import annotations

import asyncio
import time
from contextlib import aclosing

from collections import OrderedDict
from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING

import httpx

from toolang.execution.activity import ActivityQuery, ActivityReader
from toolang.execution.schemas import ActivitySnapshot
from .backend import Backend
from .roster import Roster
from .observation import HttpObservation, LocalObservation

if TYPE_CHECKING:
    from .activity_feed import HubActivityFeed


class HubActivity:
    def __init__(
        self,
        backend: Backend,
        *,
        roster: Roster | None = None,
        local_reader: Callable[[str], ActivityReader | None] | None = None,
    ) -> None:
        self.backend = backend
        self.roster = roster
        self.local_reader = local_reader
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[
            ActivityQuery, tuple[float, list[ActivitySnapshot]]
        ] = OrderedDict()
        self._feeds: dict[ActivityQuery, HubActivityFeed] = {}

    async def agents(self) -> dict[str, dict]:
        return (
            await self.roster.agents()
            if self.roster
            else await self.backend.participants()
        )

    async def local_source(
        self, agent: str, lease: dict[str, str]
    ) -> LocalObservation | None:
        if self.local_reader is None:
            return None
        if self.roster and not (await self.roster.agents()).get(agent, {}).get(
            "managed"
        ):
            return None
        reader = await asyncio.to_thread(self.local_reader, agent)
        return LocalObservation(reader, live=bool(lease)) if reader else None

    async def decorate(
        self,
        agent: str,
        pages: list[ActivitySnapshot],
        lease: dict[str, str],
        *,
        fresh: bool,
    ) -> None:
        info = (await self.roster.agents()).get(agent, {}) if self.roster else {}
        for page in pages:
            page.presence = "online" if lease else "offline"
            page.home_missing = bool(info.get("missing"))
            page.stale = not fresh or page.stale
            if page.stale or not lease:
                page.paths = []
                for node in page.roots:
                    node.stale = node.status in {"pending", "running"}

    async def updates(self, query: ActivityQuery) -> AsyncGenerator[tuple[str, dict]]:
        from .activity_feed import HubActivityFeed

        feed = self._feeds.get(query)
        if feed is None or not feed.users:
            feed = HubActivityFeed(self, query)
            self._feeds[query] = feed
        try:
            async with aclosing(feed.frames()) as updates:
                async for frame in updates:
                    yield frame
        finally:
            if not feed.users and self._feeds.get(query) is feed:
                del self._feeds[query]

    async def read(self, query: ActivityQuery) -> list[ActivitySnapshot]:
        async with self._lock:
            previous = self._cache.get(query)
            if previous and time.monotonic() - previous[0] < 0.5:
                return previous[1]
            participants = await self.agents()
            semaphore = asyncio.Semaphore(8)
            async with httpx.AsyncClient(timeout=2, trust_env=False) as http:

                async def source(agent: str) -> list[ActivitySnapshot]:
                    async with semaphore:
                        lease = await self.backend.activity.lease(agent)
                        local = await self.local_source(agent, lease)
                        if local is not None:
                            pages = await local.read(query)
                            await self.decorate(agent, pages, lease, fresh=True)
                            return pages
                        pages = []
                        fresh = False
                        if lease.get("endpoint"):
                            try:
                                pages = await HttpObservation(
                                    http, agent=agent, endpoint=lease["endpoint"]
                                ).read(query)
                                if not pages or any(
                                    page.agent != agent for page in pages
                                ):
                                    raise ValueError(
                                        "Source activity identity mismatch"
                                    )
                                fresh = await self.backend.activity.save(
                                    agent, lease["token"], pages
                                )
                                if not fresh:
                                    lease = await self.backend.activity.lease(agent)
                            except (httpx.HTTPError, ValueError):
                                pass
                        if not fresh:
                            # Exact cached queries retain their observation boundary,
                            # including historical matches and pre-filter counts.
                            pages = await self.backend.activity.cached(agent, query)
                        await self.decorate(agent, pages, lease, fresh=fresh)
                        return pages

                batches = await asyncio.gather(
                    *(
                        source(agent)
                        for agent in sorted(participants)
                        if agent.startswith("agent:")
                    )
                )
            pages = [page for batch in batches for page in batch]
            self._cache[query] = time.monotonic(), pages
            self._cache.move_to_end(query)
            while len(self._cache) > 8:
                self._cache.popitem(last=False)
            return pages
