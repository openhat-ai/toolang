"""Shared Hub activity subscriptions with independent, atomic agent updates."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress
import json
from typing import TYPE_CHECKING

import httpx
from httpx_sse import SSEError, aconnect_sse

from toolang.execution.activity import ActivityQuery
from toolang.execution.schemas import ActivitySnapshot
from .activity import query_params
from .errors import BackendUnavailable

if TYPE_CHECKING:
    from .activity import HubActivity


class HubActivityFeed:
    def __init__(self, reader: HubActivity, query: ActivityQuery) -> None:
        self.reader, self.query = reader, query
        self.pages: dict[str, list[ActivitySnapshot]] = {}
        self.versions: dict[str, int] = {}
        self.version = 0
        self.listeners: set[asyncio.Event] = set()
        self.ready = asyncio.Event()
        self.error: Exception | None = None
        self.task: asyncio.Task | None = None

    @property
    def users(self) -> int:
        return len(self.listeners)

    def changed(self) -> None:
        self.version += 1
        for listener in self.listeners:
            listener.set()

    def replace(self, agent: str, pages: list[ActivitySnapshot]) -> None:
        self.pages[agent] = pages
        self.changed()
        self.versions[agent] = self.version

    async def frames(self) -> AsyncGenerator[tuple[str, dict]]:
        wake = asyncio.Event()
        self.listeners.add(wake)
        if self.task is None:
            self.task = asyncio.create_task(self.run())
        version = -1
        agents: set[str] = set()
        try:
            await self.ready.wait()
            while True:
                wake.clear()
                if self.error:
                    raise self.error
                current = self.version
                pages = dict(self.pages)
                versions = dict(self.versions)
                first = version == -1
                if not first and agents != pages.keys():
                    yield "activity_roster", {"agents": sorted(pages)}
                for agent, batch in sorted(pages.items()):
                    if not first and versions[agent] <= version:
                        continue
                    for page in batch:
                        yield "activity_page", page.model_dump()
                    if not first:
                        yield (
                            "activity_checkpoint",
                            {"agents": [agent], "replace": False},
                        )
                if first:
                    yield "activity_checkpoint", {"agents": sorted(pages)}
                version, agents = current, set(pages)
                await wake.wait()
        finally:
            self.listeners.discard(wake)
            if not self.listeners and self.task:
                self.task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.task

    async def run(self) -> None:
        workers: dict[str, tuple[dict[str, str], asyncio.Task]] = {}
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(2, read=20), trust_env=False
            ) as http:
                while True:
                    participants = await self.reader.agents()
                    agents = {
                        name for name in participants if name.startswith("agent:")
                    }
                    for agent in set(self.pages) - agents:
                        self.pages.pop(agent)
                        self.versions.pop(agent)
                        self.changed()
                    for agent in set(workers) - agents:
                        _, task = workers.pop(agent)
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    for agent in sorted(agents):
                        lease = await self.reader.backend.lease(agent)
                        previous = workers.get(agent)
                        if previous and (previous[0] != lease or previous[1].done()):
                            previous[1].cancel()
                            await asyncio.gather(previous[1], return_exceptions=True)
                            del workers[agent]
                        if (
                            agent not in self.pages
                            or previous
                            and previous[0] != lease
                            or not lease.get("endpoint")
                        ):
                            pages = await self.reader.backend.cached(agent, self.query)
                            await self.reader.decorate(agent, pages, lease, fresh=False)
                            self.replace(agent, pages)
                        if lease.get("endpoint") and agent not in workers:
                            workers[agent] = (
                                lease,
                                asyncio.create_task(self.source(http, agent, lease)),
                            )
                    self.ready.set()
                    await asyncio.sleep(1)
        except Exception as exc:
            self.error = exc
            self.ready.set()
            self.changed()
        finally:
            for _, task in workers.values():
                task.cancel()
            await asyncio.gather(
                *(task for _, task in workers.values()), return_exceptions=True
            )

    async def source(
        self, http: httpx.AsyncClient, agent: str, lease: dict[str, str]
    ) -> None:
        delay = 0.5
        while True:
            try:
                async with aconnect_sse(
                    http,
                    "GET",
                    lease["endpoint"] + "/api/v1/activity/stream",
                    params=query_params(self.query),
                ) as source:
                    source.response.raise_for_status()
                    pages: list[ActivitySnapshot] = []
                    async for event in source.aiter_sse():
                        if event.event == "activity_page":
                            pages.append(
                                ActivitySnapshot.model_validate_json(event.data)
                            )
                        elif event.event == "activity_checkpoint":
                            if json.loads(event.data)["agents"] != [agent]:
                                raise ValueError("Source activity identity mismatch")
                            batch, pages = pages, []
                            saved = await self.reader.backend.save(
                                agent, lease["token"], batch
                            )
                            if (
                                not saved
                                and await self.reader.backend.lease(agent) != lease
                            ):
                                return
                            # Another reader may have cached a later observation.
                            # That must not block this feed's ordered live frames.
                            previous = self.pages.get(agent)
                            if (
                                previous
                                and previous[0].session == batch[0].session
                                and (batch[0].revision, batch[0].observed or 0)
                                < (previous[0].revision, previous[0].observed or 0)
                            ):
                                continue
                            await self.reader.decorate(agent, batch, lease, fresh=True)
                            self.replace(agent, batch)
                            delay = 0.5
                    raise httpx.ReadError("Source activity disconnected")
            except (
                httpx.HTTPError,
                SSEError,
                ValueError,
                KeyError,
                BackendUnavailable,
            ):
                pages = [
                    page.model_copy(deep=True) for page in self.pages.get(agent, [])
                ]
                try:
                    await self.reader.decorate(agent, pages, lease, fresh=False)
                except BackendUnavailable:
                    for page in pages:
                        page.presence = "unknown"
                        page.stale = True
                        page.paths = []
                self.replace(agent, pages)
                await asyncio.sleep(delay)
                delay = min(5, delay * 2)
