"""Portable asynchronous relay for a workload log file."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys


async def start_file_follower(path: Path) -> asyncio.Task[None]:
    """Start relaying content appended after this call to stderr."""

    position = await asyncio.to_thread(_size, path)
    return asyncio.create_task(_follow(path, position))


async def stop_file_follower(task: asyncio.Task[None]) -> None:
    """Flush and stop one file relay."""

    if task.done():
        await asyncio.gather(task, return_exceptions=True)
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _follow(path: Path, position: int) -> None:
    try:
        while True:
            position = await asyncio.to_thread(_relay_new, path, position)
            await asyncio.sleep(0.05)
    except asyncio.CancelledError:
        await asyncio.to_thread(_relay_new, path, position)
        raise


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _relay_new(path: Path, position: int) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(position)
            content = stream.read()
            next_position = stream.tell()
    except OSError:
        return position
    if content:
        sys.stderr.write(content)
        sys.stderr.flush()
    return next_position
