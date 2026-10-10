"""Hub activity federation from local stores, agent HTTP and cached coverage."""

from __future__ import annotations

import asyncio
from contextlib import aclosing

from collections import OrderedDict
import hashlib
import json
import sqlite3
import time
from typing import Annotated
from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.sse import EventSourceResponse, ServerSentEvent

from toolang.execution.activity import ActivityQuery, ActivityReader
from toolang.execution.schemas import ActivityMetrics, ActivitySnapshot
from .backend import Backend, PREFIX, LAST_SEEN, online_key
from .roster import Roster
from .observation import HttpObservation, LocalObservation

if TYPE_CHECKING:
    from .activity_feed import HubActivityFeed


def activity_query(
    since: str = "session",
    recent: Annotated[float | None, Query(gt=0)] = 1800,
    text: Annotated[str, Query(alias="filter", max_length=240)] = "",
    active: bool = False,
    all_recent: bool = False,
) -> ActivityQuery:
    try:
        return ActivityQuery(since, None if all_recent else recent, text, active)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class ActivityBackend:
    """Backend boundary for absolute snapshots, fenced by the existing agent lease."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend

    async def lease(self, agent: str) -> dict[str, str]:
        return await self.backend._call("HGETALL", online_key(agent))

    async def save(self, agent: str, token: str, pages: list[ActivitySnapshot]) -> bool:
        if not pages or any(page.agent != agent for page in pages):
            raise ValueError("Activity snapshot belongs to another agent")
        first = pages[0]
        expected = 0
        for page in pages:
            if page.observed is None:
                raise ValueError("Published activity requires an observation time")
            if page.offset != expected or (
                page.session,
                page.revision,
                self.key(page),
            ) != (first.session, first.revision, self.key(first)):
                raise ValueError("Activity pages must share one boundary and query")
            expected = page.next_offset
        if expected is not None:
            raise ValueError("Activity publication is missing pages")
        query = self.key(first)
        # Keep the default publication plus the last requested range. Older
        # queries need a live source; never mislabel another range's statistics.
        slot = "default" if query == self.key(ActivityQuery()) else "query"
        return bool(
            await self.backend._eval(
                """
            if redis.call('HGET',KEYS[1],'token')~=ARGV[1] then return 0 end
            local old=redis.call('HGET',KEYS[2],ARGV[2])
            if old then
              local a=cjson.decode(old); local b=cjson.decode(ARGV[3])
              if a.query==b.query and a.pages[1].session==b.pages[1].session then
                if a.pages[1].revision>b.pages[1].revision or
                  (a.pages[1].revision==b.pages[1].revision and a.pages[1].observed>b.pages[1].observed)
                then return 0 end
              end
            end
            redis.call('HSET',KEYS[2],ARGV[2],ARGV[3]); return 1
            """,
                [online_key(agent), f"{PREFIX}:activity:{agent}"],
                [
                    token,
                    slot,
                    json.dumps(
                        {
                            "query": query,
                            "pages": [page.model_dump() for page in pages],
                        },
                        separators=(",", ":"),
                    ),
                ],
            )
        )

    @staticmethod
    def key(query: ActivityQuery | ActivitySnapshot) -> str:
        fields = (
            (
                query.since,
                float(query.recent) if query.recent is not None else None,
                query.text,
                query.active,
            )
            if isinstance(query, ActivityQuery)
            else (
                query.since,
                float(query.recent) if query.recent is not None else None,
                query.filter,
                query.active_only,
            )
        )
        return hashlib.sha256(json.dumps(fields).encode()).hexdigest()

    async def cached(self, agent: str, query: ActivityQuery) -> list[ActivitySnapshot]:
        rows = await self.backend._call("HGETALL", f"{PREFIX}:activity:{agent}")
        fallback = None
        for slot in ("query", "default"):
            if slot not in rows:
                continue
            data = json.loads(rows[slot])
            pages = [ActivitySnapshot.model_validate(page) for page in data["pages"]]
            if data["query"] == self.key(query):
                return pages
            if slot == "default":
                fallback = pages
        if fallback:
            fallback = cached_selection(fallback, query)
            for page in fallback:
                page.complete = False
                page.coverage = (
                    "Requested range unavailable offline; showing last cached activity"
                )
                if page.since != query.since:
                    page.stats = ActivityMetrics(
                        model=None, tool=None, cost=None, time=None, complete=False
                    )
                    for node in [*page.threads, *page.roots, *page.paths]:
                        node.stats = page.stats
                page.since = query.since
                page.recent = query.recent
                page.filter = query.text
                page.active_only = query.active
            return fallback
        unknown = ActivityMetrics(
            model=None, tool=None, cost=None, time=None, complete=False
        )
        return [
            ActivitySnapshot(
                agent=agent,
                revision=0,
                observed=None,
                since=query.since,
                recent=query.recent,
                filter=query.text,
                active_only=query.active,
                complete=False,
                coverage="No activity snapshot available",
                stats=unknown,
                total=unknown,
            )
        ]


def cached_selection(
    pages: list[ActivitySnapshot], query: ActivityQuery
) -> list[ActivitySnapshot]:
    """Apply narrower visibility to cached metadata without changing frozen Stats."""
    first = pages[0].model_copy(deep=True)
    roots = [node for page in pages for node in page.roots]
    paths = [node for page in pages for node in page.paths]
    cutoff = time.time() - query.recent if query.recent is not None else 0
    eligible = [
        node
        for node in roots
        if node.status in {"pending", "running"} or node.changed >= cutoff
    ]
    text = query.text.casefold()
    matches = set()
    for node in [*eligible, *paths]:
        if any(
            text in str(value).casefold()
            for value in (
                first.agent,
                node.thread,
                node.id,
                node.title,
                node.summary,
                node.status,
            )
        ):
            matches.add(node.root)
    roots = [
        node
        for node in eligible
        if (not query.active or node.status in {"pending", "running"})
        and (not text or node.root in matches)
    ]
    first.roots = roots
    first.paths = []
    first.active = sum(node.status in {"pending", "running"} for node in eligible)
    first.failed = sum(node.status == "failed" for node in eligible)
    first.eligible, first.matched, first.available = (
        len(eligible),
        len(roots),
        len(roots),
    )
    first.offset, first.next_offset = 0, None
    for thread in first.threads:
        thread.active = sum(
            node.thread == thread.id and node.status in {"pending", "running"}
            for node in eligible
        )
        thread.failed = sum(
            node.thread == thread.id and node.status == "failed" for node in eligible
        )
    eligible_threads = [
        thread for thread in first.threads if thread.active or thread.changed >= cutoff
    ]
    first.threads = [
        thread
        for thread in eligible_threads
        if (not query.active or thread.active)
        and (
            not text
            or text in first.agent.casefold()
            or text in thread.id.casefold()
            or any(node.thread == thread.id for node in roots)
        )
    ]
    first.thread_eligible = len(eligible_threads)
    first.thread_matched = len(first.threads)
    return [first]


class HubActivity:
    def __init__(
        self,
        backend: Backend,
        *,
        roster: Roster | None = None,
        local_reader: Callable[[str], ActivityReader | None] | None = None,
    ) -> None:
        self.backend = ActivityBackend(backend)
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
            else await self.backend.backend.participants()
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
        seen = await self.backend.backend._call("HGET", LAST_SEEN, agent)
        info = (await self.roster.agents()).get(agent, {}) if self.roster else {}
        for page in pages:
            page.presence = "online" if lease else "offline"
            page.last_seen = float(seen) if seen else None
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
                        lease = await self.backend.lease(agent)
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
                                fresh = await self.backend.save(
                                    agent, lease["token"], pages
                                )
                                if not fresh:
                                    lease = await self.backend.lease(agent)
                            except (httpx.HTTPError, ValueError):
                                pass
                        if not fresh:
                            # Exact cached queries retain their observation boundary,
                            # including historical matches and pre-filter counts.
                            pages = await self.backend.cached(agent, query)
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


def activity_router(
    backend: Backend,
    *,
    roster: Roster | None = None,
    local_reader: Callable[[str], ActivityReader | None] | None = None,
) -> APIRouter:
    reader = HubActivity(backend, roster=roster, local_reader=local_reader)
    router = APIRouter(prefix="/activity", tags=["activity"])

    @router.get("")
    async def snapshot(
        query: Annotated[ActivityQuery, Depends(activity_query)],
    ) -> list[ActivitySnapshot]:
        return await reader.read(query)

    @router.get("/stream", response_class=EventSourceResponse)
    async def stream(
        query: Annotated[ActivityQuery, Depends(activity_query)],
    ) -> AsyncGenerator[ServerSentEvent]:
        async with aclosing(reader.updates(query)) as updates:
            async for event, data in updates:
                yield ServerSentEvent(event=event, data=data)

    @router.get("/result")
    async def result(agent: str, ref: str) -> dict[str, str]:
        if agent not in await reader.agents():
            raise HTTPException(404, "Agent not found")
        lease = await reader.backend.lease(agent)
        local = await reader.local_source(agent, lease)
        if local is not None:
            try:
                return {"text": await local.result(agent, ref)}
            except KeyError as exc:
                raise HTTPException(404, "Execution record not found") from exc
            except (OSError, sqlite3.Error) as exc:
                raise HTTPException(503, "Result unavailable") from exc
            except ValueError as exc:
                raise HTTPException(422, "Result unavailable") from exc
        if not lease.get("endpoint"):
            raise HTTPException(503, "Agent is offline; use inspect against its home")
        try:
            async with httpx.AsyncClient(timeout=2, trust_env=False) as http:
                response = await http.get(
                    lease["endpoint"] + "/api/v1/activity/result", params={"ref": ref}
                )
                response.raise_for_status()
                if lease != await reader.backend.lease(agent):
                    raise HTTPException(409, "Agent restarted; reopen Details")
                return {"text": response.json()["text"]}
        except httpx.HTTPStatusError as exc:
            raise HTTPException(exc.response.status_code, "Result unavailable") from exc
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise HTTPException(502, "Result unavailable") from exc

    return router
