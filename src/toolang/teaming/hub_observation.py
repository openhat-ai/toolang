"""Current Hub snapshots refreshed by independent execution and member signals."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from pydantic import TypeAdapter
import httpx

from toolang.execution.activity import ActivityQuery
from toolang.execution.schemas import ActivitySnapshot
from .activity_cache import select_cached
from .notifications import Changes
from .observation import Frame, HttpObservation, Presence, frames, query_params

_ROSTER = TypeAdapter(dict[str, Presence])


class HubObservation(HttpObservation):
    async def updates(self, query: ActivityQuery) -> AsyncGenerator[Frame]:
        pages: dict[str, list[ActivitySnapshot]] = {}
        roster: dict[str, Presence] = {}
        workers: dict[str, tuple[asyncio.Event, asyncio.Task]] = {}
        available: set[str] = set()
        membership_known = False
        received_roster = False
        changed = asyncio.Event()

        async with (
            Changes(
                self.http,
                "/team/events",
                checkpoint="checkpoint",
                matches=lambda event, data: (
                    event == "change"
                    and str(data.get("type", "")).startswith(("team.", "presence."))
                ),
            ) as members,
            Changes(
                self.http,
                "/events/stream",
                checkpoint="stream_checkpoint",
                matches=lambda event, data: not event.startswith("part_"),
            ) as execution,
        ):

            async def activity(agent: str, dirty: asyncio.Event) -> None:
                while True:
                    await dirty.wait()
                    dirty.clear()
                    if execution.connected:
                        try:
                            response = await self.http.get(
                                self.path,
                                params={**query_params(query), "agent": agent},
                                timeout=10,
                            )
                            response.raise_for_status()
                            batch = [
                                ActivitySnapshot.model_validate(value)
                                for value in response.json()
                            ]
                            if any(page.agent != agent for page in batch):
                                raise ValueError("Activity source identity mismatch")
                            pages[agent] = batch
                            available.add(agent)
                        except (httpx.HTTPError, ValueError, TypeError):
                            available.discard(agent)
                    else:
                        available.discard(agent)
                    changed.set()

            async def membership() -> None:
                nonlocal roster, membership_known, received_roster
                while True:
                    await members.wait(timeout=5)
                    if members.connected:
                        try:
                            response = await self.http.get(
                                "/activity/roster", timeout=3
                            )
                            response.raise_for_status()
                            roster = _ROSTER.validate_python(response.json())
                            membership_known = True
                            received_roster = True
                        except (httpx.HTTPError, ValueError):
                            membership_known = False
                    else:
                        membership_known = False
                    for agent in workers.keys() - roster.keys():
                        _, task = workers.pop(agent)
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        pages.pop(agent, None)
                        available.discard(agent)
                    for agent in roster:
                        if agent not in workers:
                            dirty = asyncio.Event()
                            workers[agent] = (
                                dirty,
                                asyncio.create_task(activity(agent, dirty)),
                            )
                        workers[agent][0].set()
                    changed.set()

            async def ticks() -> None:
                while True:
                    # Clock/window changes and agents without an exported execution
                    # projection still need current activity snapshots.
                    await execution.wait(timeout=1)
                    for dirty, _ in workers.values():
                        dirty.set()
                    changed.set()

            tasks = [asyncio.create_task(membership()), asyncio.create_task(ticks())]
            for task in tasks:
                task.add_done_callback(lambda _: changed.set())
            try:
                while True:
                    await changed.wait()
                    changed.clear()
                    for task in tasks:
                        if task.done():
                            await task
                    current = []
                    for agent, presence in roster.items():
                        batch = pages.get(agent) or select_cached(agent, query, {})
                        for original in batch:
                            page = original.model_copy(deep=True)
                            page.presence = (
                                presence
                                if members.connected and membership_known
                                else "unknown"
                            )
                            if agent not in available or not execution.connected:
                                page.stale = True
                                page.coverage = "Activity unavailable; reconnecting"
                            if page.presence != "online" or page.stale:
                                page.paths = []
                                for node in page.roots:
                                    node.stale |= node.status in {"pending", "running"}
                            current.append(page)
                    if received_roster:
                        for frame in frames(current):
                            yield frame
                    yield (
                        "observation_status",
                        {
                            "reconnecting": not (
                                members.connected
                                and membership_known
                                and execution.connected
                                and set(roster) <= available
                            )
                        },
                    )
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for _, task in workers.values():
                    task.cancel()
                await asyncio.gather(
                    *(task for _, task in workers.values()), return_exceptions=True
                )
