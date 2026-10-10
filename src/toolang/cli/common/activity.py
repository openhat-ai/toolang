"""Activity observation and terminal lifecycle; never acquires an executor."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, aclosing, closing, suppress
import math
import signal
import sys
import time
import sqlite3
from typing import TextIO, cast

import httpx
from prompt_toolkit.input import create_input
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.output import create_output
from rich.console import Console
from rich.live import Live

from toolang.execution.activity import ActivityQuery
from toolang.teaming.observation import HttpObservation, Observation, frames
from .activity_view import Activity, Sort, View
from .terminal_surfaces import resolve_terminal_surfaces

__all__ = ["Activity", "watch"]


async def watch(
    endpoint: str | None,
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
    refresh: float = 0.1,
    source: Observation | None = None,
) -> None:
    if not math.isfinite(refresh) or refresh <= 0:
        raise ValueError("Refresh must be a finite positive number of seconds")
    state = Activity(
        agent,
        view=view,
        tree=tree,
        sort=sort,
        query=query,
        recent_label=recent_label,
        refresh=refresh,
        surfaces=resolve_terminal_surfaces(
            output_stream=cast(TextIO, console.file), probe=not once
        ),
    )
    stop = asyncio.Event()
    changed = asyncio.Event()
    redraw = asyncio.Event()
    needs_render = True
    async with AsyncExitStack() as stack:
        if source is None:
            if endpoint is None:
                raise ValueError("Activity source is required")
            http = await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=endpoint,
                    headers={"X-Toolang-Backend": backend} if backend else {},
                    timeout=httpx.Timeout(10, read=None),
                    trust_env=False,
                )
            )
            source = HttpObservation(http, agent=agent)
        observer = source
        if once:
            try:
                for event, data in frames(await observer.read(state.query)):
                    state.feed(event, data)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                raise ValueError(f"Activity service unavailable: {exc}") from exc
            console.print(
                state.render(width=console.width, height=console.height, once=True)
            )
            return

        async def receive() -> None:
            nonlocal needs_render
            delay = 0.5
            while not stop.is_set():
                state.attach()
                try:
                    async with aclosing(
                        observer.updates(state.attached_query)
                    ) as updates:
                        async for event, data in updates:
                            needs_render |= state.feed(event, data)
                            delay = 0.5
                except httpx.HTTPError:
                    state.reconnecting = True
                    needs_render = True
                try:
                    await asyncio.wait_for(stop.wait(), delay)
                except TimeoutError:
                    pass
                delay = min(5, delay * 2)

        with closing(create_input(stdin=sys.stdin)) as terminal:
            loop = asyncio.get_running_loop()
            flush_handle: asyncio.TimerHandle | None = None

            def apply_keys(events: list[KeyPress]) -> None:
                for event in events:
                    if state.key(event.key, event.data):
                        stop.set()
                    if state.dirty:
                        state.dirty = False
                        changed.set()
                if terminal.closed:
                    stop.set()
                if events or terminal.closed:
                    redraw.set()

            def keys() -> None:
                nonlocal flush_handle
                apply_keys(terminal.read_keys())
                # Escape is also an escape-sequence prefix. Flush it after a
                # short idle interval, allowing split function/arrow sequences.
                if flush_handle is not None:
                    flush_handle.cancel()
                flush_handle = loop.call_later(
                    0.1, lambda: apply_keys(terminal.flush_keys())
                )

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
                output = create_output(stdout=cast(TextIO, console.file))
                output.enable_bracketed_paste()
                output.flush()
                task = asyncio.create_task(observe())
                task.add_done_callback(lambda _: redraw.set())
                result_task: asyncio.Task | None = None

                async def fetch_result(key: tuple[str, str, str]) -> None:
                    nonlocal needs_render
                    try:
                        text = await observer.result(key[0], key[1]) or "Pending"
                    except (
                        httpx.HTTPError,
                        OSError,
                        sqlite3.Error,
                        ValueError,
                        KeyError,
                    ):
                        text = "Unavailable"
                    if state.result_key == key:
                        state.result_text = text
                        needs_render = True

                old_resize = signal.getsignal(signal.SIGWINCH)
                resize_installed = False
                try:
                    signal.signal(signal.SIGWINCH, lambda *_: redraw.set())
                    resize_installed = True
                except ValueError:
                    pass  # Embedded callers may run outside the main thread.
                deadline = loop.time()
                clock = -1
                try:
                    while not stop.is_set():
                        if task.done():
                            await task
                        current_clock = int(time.time())
                        needs_render |= current_clock != clock
                        immediate = redraw.is_set()
                        if immediate or loop.time() >= deadline:
                            redraw.clear()
                            if immediate or needs_render:
                                current = next(
                                    (
                                        row
                                        for row in state.rows()
                                        if row.key == state.selected
                                    ),
                                    None,
                                )
                                target = (
                                    (current.agent, current.id, current.node.status)
                                    if state.details
                                    and current
                                    and current.node
                                    and current.node.kind != "thread"
                                    else None
                                )
                                if target != state.result_key:
                                    if result_task:
                                        result_task.cancel()
                                        await asyncio.gather(
                                            result_task, return_exceptions=True
                                        )
                                    (
                                        state.result_key,
                                        state.result_text,
                                        state.details_offset,
                                    ) = target, "Loading", 0
                                    result_task = (
                                        asyncio.create_task(fetch_result(target))
                                        if target
                                        else None
                                    )
                                live.update(
                                    state.render(
                                        width=console.width, height=console.height
                                    ),
                                    refresh=True,
                                )
                                needs_render = False
                                clock = current_clock
                            deadline = loop.time() + refresh
                        try:
                            await asyncio.wait_for(
                                redraw.wait(), max(0, deadline - loop.time())
                            )
                        except TimeoutError:
                            pass
                finally:
                    if resize_installed:
                        signal.signal(signal.SIGWINCH, old_resize)
                    if flush_handle is not None:
                        flush_handle.cancel()
                    output.disable_bracketed_paste()
                    output.flush()
                    stop.set()
                    if result_task:
                        result_task.cancel()
                        await asyncio.gather(result_task, return_exceptions=True)
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
