"""Shallow directory events with an independent reconciliation deadline."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from pathlib import Path

from watchfiles import awatch


async def directory_changes(
    directories: Callable[[], tuple[Path, ...]],
    *,
    stop_signal: asyncio.Event,
    interval_ms: float,
    debounce_ms: float = 50,
) -> AsyncGenerator[frozenset[Path], None]:
    """Rebuild changing subscriptions and yield periodic checks even when busy."""
    interval = max(interval_ms, 50) / 1000
    loop = asyncio.get_running_loop()
    deadline = loop.time() + interval
    stop = asyncio.create_task(stop_signal.wait())
    try:
        while not stop_signal.is_set():
            selected = directories()
            if not selected:
                await asyncio.wait({stop}, timeout=interval)
                yield frozenset()
                continue
            watch_stop = asyncio.Event()
            changes = awatch(
                *selected,
                recursive=False,
                debounce=max(int(debounce_ms), 50),
                step=50,
                rust_timeout=max(int(interval_ms), 50),
                stop_event=watch_stop,
            )
            pending = asyncio.create_task(anext(changes))
            try:
                # awatch installs native subscriptions before its first await.
                await asyncio.sleep(0)
                yield frozenset()
                while not stop_signal.is_set():
                    done, _ = await asyncio.wait(
                        {pending, stop},
                        timeout=max(0, deadline - loop.time()),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stop in done:
                        return
                    paths = frozenset()
                    if pending in done:
                        try:
                            paths = frozenset(
                                Path(path) for _, path in pending.result()
                            )
                        except (StopAsyncIteration, FileNotFoundError):
                            break
                        pending = asyncio.create_task(anext(changes))
                    due = loop.time() >= deadline
                    if due:
                        deadline = loop.time() + interval
                    if paths or due:
                        yield paths
                    if directories() != selected:
                        break
            finally:
                watch_stop.set()
                pending.cancel()
                with suppress(
                    asyncio.CancelledError, StopAsyncIteration, FileNotFoundError
                ):
                    await pending
                await changes.aclose()
    finally:
        stop.cancel()
        with suppress(asyncio.CancelledError):
            await stop
