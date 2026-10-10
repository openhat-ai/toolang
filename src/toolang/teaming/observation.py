"""Reusable read-only activity sources, independent of terminal presentation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
import json
import sqlite3
from typing import Literal, Protocol

import httpx
from httpx_sse import aconnect_sse

from toolang.execution.activity import ActivityQuery, ActivityReader
from toolang.execution.schemas import ActivityMetrics, ActivitySnapshot

Presence = Literal["online", "offline", "unknown"]
Frame = tuple[str, dict]


class Observation(Protocol):
    async def read(self, query: ActivityQuery) -> list[ActivitySnapshot]: ...
    def updates(self, query: ActivityQuery) -> AsyncGenerator[Frame]: ...
    async def result(self, agent: str, ref: str) -> str: ...


def query_params(query: ActivityQuery) -> dict[str, str]:
    return {
        "since": query.since,
        "recent": str(query.recent or 1800),
        "all_recent": str(query.recent is None).lower(),
        "filter": query.text,
        "active": str(query.active).lower(),
    }


def frames(pages: list[ActivitySnapshot]) -> list[Frame]:
    return [
        *(("activity_page", page.model_dump()) for page in pages),
        ("activity_checkpoint", {"agents": sorted({p.agent for p in pages})}),
    ]


def totals(pages: list[ActivitySnapshot]) -> ActivityMetrics:
    """Aggregate each agent once, retaining unknown and partial coverage."""
    metrics = [page.stats for page in pages if page.offset == 0]
    return ActivityMetrics.model_validate(
        {
            **{
                field: sum(getattr(item, field) or 0 for item in metrics)
                if any(getattr(item, field) is not None for item in metrics)
                else None
                for field in (
                    "model",
                    "tool",
                    "input_tokens",
                    "cached_tokens",
                    "output_tokens",
                    "cost",
                    "time",
                )
            },
            "estimated": any(m.estimated for m in metrics),
            "partial": any(m.partial or m.cost is None for m in metrics),
            "complete": bool(metrics) and all(m.complete for m in metrics),
            "tokens_complete": bool(metrics)
            and all(m.tokens_complete for m in metrics),
        }
    )


class LocalObservation:
    """Read a supplied store without creating it or connecting to a Hub backend."""

    def __init__(
        self,
        reader: ActivityReader,
        *,
        presence: Callable[[], Presence] | None = None,
        live: bool | None = None,
    ) -> None:
        self.reader = reader
        self.presence = presence
        self.live = live

    async def read(self, query: ActivityQuery) -> list[ActivitySnapshot]:
        presence = await asyncio.to_thread(self.presence) if self.presence else None
        try:
            pages = await asyncio.to_thread(
                self.reader.pages,
                query,
                live=self.live if presence is None else presence == "online",
            )
        except (OSError, sqlite3.Error, ValueError):
            unknown = ActivityMetrics(
                model=None, tool=None, cost=None, time=None, complete=False
            )
            return [
                ActivitySnapshot(
                    agent=self.reader.agent,
                    revision=0,
                    observed=None,
                    since=query.since,
                    recent=query.recent,
                    filter=query.text,
                    active_only=query.active,
                    presence=presence or "unknown",
                    stale=True,
                    complete=False,
                    coverage="History unavailable",
                    stats=unknown,
                    total=unknown,
                )
            ]
        if presence is not None:
            for page in pages:
                page.presence = presence
        return pages

    async def updates(self, query: ActivityQuery) -> AsyncGenerator[Frame]:
        while True:
            presence = await asyncio.to_thread(self.presence) if self.presence else None
            live = self.live if presence is None else presence == "online"
            try:
                async with aclosing(self.reader.updates(query, live=live)) as updates:
                    async for pages in updates:
                        if self.presence:
                            observed_presence = await asyncio.to_thread(self.presence)
                            if (observed_presence == "online") != live:
                                break
                            pages = [
                                page.model_copy(update={"presence": observed_presence})
                                for page in pages
                            ]
                        for frame in frames(pages):
                            yield frame
            except (OSError, sqlite3.Error, ValueError):
                for frame in frames(await self.read(query)):
                    yield frame
                await asyncio.sleep(1)

    async def result(self, agent: str, ref: str) -> str:
        if agent != self.reader.agent:
            raise ValueError("Activity source identity mismatch")
        return await asyncio.to_thread(self.reader.result, ref)


class HttpObservation:
    """HTTP adapter for the same snapshot, update and result interface."""

    def __init__(
        self, http: httpx.AsyncClient, *, agent: str | None = None, endpoint: str = ""
    ) -> None:
        self.http = http
        self.agent = agent
        self.path = endpoint.rstrip("/") + (
            "/api/v1/activity" if agent else "/activity"
        )

    async def read(self, query: ActivityQuery) -> list[ActivitySnapshot]:
        response = await self.http.get(
            self.path + ("/batch" if self.agent else ""), params=query_params(query)
        )
        response.raise_for_status()
        return [ActivitySnapshot.model_validate(value) for value in response.json()]

    async def updates(self, query: ActivityQuery) -> AsyncGenerator[Frame]:
        async with aconnect_sse(
            self.http, "GET", self.path + "/stream", params=query_params(query)
        ) as source:
            if source.response.status_code in {400, 401, 403, 404, 409, 422}:
                raise ValueError("Activity request rejected; check the running service")
            source.response.raise_for_status()
            async for event in source.aiter_sse():
                if event.data:
                    if event.event == "stream_error":
                        raise httpx.ReadError("Activity source is recovering")
                    yield event.event, json.loads(event.data)
        raise httpx.ReadError("Activity stream disconnected")

    async def result(self, agent: str, ref: str) -> str:
        response = await self.http.get(
            self.path + "/result", params={"agent": agent, "ref": ref}, timeout=3
        )
        response.raise_for_status()
        return response.json()["text"]
