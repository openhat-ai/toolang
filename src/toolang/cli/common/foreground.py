"""Run foreground CLI work with one interruption and finalization budget."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import signal
import threading
from types import FrameType
from typing import Any, TypeVar, cast

from toolang.up.closing import ClosingDeadline

_T = TypeVar("_T")


def run_foreground(
    work: Coroutine[Any, Any, _T],
    *,
    closing: ClosingDeadline | None = None,
    interrupt_reason: str = "script interrupted",
) -> _T:
    """Start the deadline before Runner begins cancelling setup or execution."""
    if closing is None:
        return asyncio.run(work)

    async def guarded() -> _T:
        previous = signal.getsignal(signal.SIGINT)
        installed = (
            threading.current_thread() is threading.main_thread()
            and previous != signal.SIG_IGN
        )
        interrupted = False

        def on_interrupt(sig, frame) -> None:
            nonlocal interrupted
            if interrupted:
                return
            interrupted = True
            try:
                closing.begin(interrupt_reason)
            finally:
                if callable(previous):
                    cast(Callable[[int, FrameType | None], Any], previous)(sig, frame)
                else:
                    raise KeyboardInterrupt

        if installed:
            signal.signal(signal.SIGINT, on_interrupt)
        try:
            return await work
        finally:
            if installed and signal.getsignal(signal.SIGINT) is on_interrupt:
                signal.signal(signal.SIGINT, previous)

    return asyncio.run(guarded())
