"""HTTP activity observation and terminal lifecycle; never acquires an executor."""

from __future__ import annotations

import asyncio
from contextlib import closing, suppress
import json
import sys

import httpx
from httpx_sse import aconnect_sse
from prompt_toolkit.input import create_input
from rich.console import Console
from rich.live import Live

from toolang.execution.activity import ActivityQuery
from .activity_view import Activity, Sort, View

__all__ = ["Activity", "watch"]


def parameters(query: ActivityQuery) -> dict[str, str]:
    return {
        "since": query.since,
        "recent": str(query.recent or 1800),
        "all_recent": str(query.recent is None).lower(),
        "filter": query.text,
        "active": str(query.active).lower(),
    }


async def watch(
    endpoint: str,
    *,
    agent: str | None,
    backend: str | None,
    once: bool,
    console: Console,
    view: View | None = None,
    tree: bool = False,
    sort: Sort = "activity",
    query: ActivityQuery | None = None,
    recent_label: str = "30m",
) -> None:
    state = Activity(
        agent, view=view, tree=tree, sort=sort, query=query, recent_label=recent_label
    )
    stop = asyncio.Event()
    changed = asyncio.Event()
    path = "/api/v1/activity" if agent else "/activity"
    async with httpx.AsyncClient(
        base_url=endpoint,
        headers={"X-Toolang-Backend": backend} if backend else {},
        timeout=httpx.Timeout(10, read=None),
        trust_env=False,
    ) as http:
        if once:
            try:
                response = await http.get(
                    path + ("/batch" if agent else ""), params=parameters(state.query)
                )
                response.raise_for_status()
                pages = response.json()
                for page in pages:
                    state.feed("activity_page", page)
                state.feed(
                    "activity_checkpoint",
                    {"agents": sorted({page["agent"] for page in pages})},
                )
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                raise ValueError(f"Activity service unavailable: {exc}") from exc
            console.print(
                state.render(width=console.width, height=console.height, once=True)
            )
            return

        async def receive() -> None:
            delay = 0.5
            while not stop.is_set():
                state.attach()
                try:
                    async with aconnect_sse(
                        http, "GET", path + "/stream", params=parameters(state.query)
                    ) as source:
                        if source.response.status_code in {
                            400,
                            401,
                            403,
                            404,
                            409,
                            422,
                        }:
                            raise ValueError(
                                f"Activity request rejected ({source.response.status_code}); check the running service"
                            )
                        source.response.raise_for_status()
                        async for event in source.aiter_sse():
                            if event.data:
                                if event.event == "stream_error":
                                    raise httpx.ReadError(
                                        "Activity source is recovering"
                                    )
                                state.feed(event.event, json.loads(event.data))
                                delay = 0.5
                        raise httpx.ReadError("Activity stream disconnected")
                except httpx.HTTPError:
                    state.reconnecting = True
                try:
                    await asyncio.wait_for(stop.wait(), delay)
                except TimeoutError:
                    pass
                delay = min(5, delay * 2)

        with closing(create_input(stdin=sys.stdin)) as terminal:

            def keys() -> None:
                for event in terminal.read_keys():
                    if state.key(event.key):
                        stop.set()
                    if state.dirty:
                        state.dirty = False
                        changed.set()

            async def observe() -> None:
                while not stop.is_set():
                    changed.clear()
                    receiver = asyncio.create_task(receive())
                    update = asyncio.create_task(changed.wait())
                    ending = asyncio.create_task(stop.wait())
                    try:
                        done, _ = await asyncio.wait(
                            {receiver, update, ending},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if receiver in done:
                            await receiver
                    finally:
                        for task in (receiver, update, ending):
                            task.cancel()
                        await asyncio.gather(
                            receiver, update, ending, return_exceptions=True
                        )

            with (
                terminal.raw_mode(),
                terminal.attach(keys),
                Live(console=console, auto_refresh=False, screen=True) as live,
            ):
                task = asyncio.create_task(observe())
                try:
                    while not stop.is_set():
                        if task.done():
                            await task
                        live.update(
                            state.render(width=console.width, height=console.height),
                            refresh=True,
                        )
                        try:
                            await asyncio.wait_for(stop.wait(), 0.5)
                        except TimeoutError:
                            pass
                finally:
                    stop.set()
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
