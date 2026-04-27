"""Reload loop that applies the latest prepared snapshot to live state."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from ..state.live import load_live_state
from ..state.prepared import load_prepared_state

if TYPE_CHECKING:
    from ..up import UptimeContext

DEFAULT_DEBOUNCE_MS = 500.0
logger = logging.getLogger("toolang.loop.reload")


def spawn(
    context: UptimeContext,
    *,
    stop_signal: asyncio.Event,
    reload_signal: asyncio.Event,
) -> asyncio.Task[None]:
    """Spawn the reload loop in one background task."""

    return asyncio.create_task(run(context, stop_signal=stop_signal, reload_signal=reload_signal))


async def run(
    context: UptimeContext,
    *,
    stop_signal: asyncio.Event,
    reload_signal: asyncio.Event,
) -> None:
    """Apply the latest prepared snapshot to live state after debounce."""
    debounce_value = context.config.require("loops.reload.debounce_ms")
    if not isinstance(debounce_value, int | float):
        raise TypeError("invalid config: loops.reload.debounce_ms")
    debounce_timeout = float(debounce_value) / 1000
    logger.debug(
        "reload loop started root=%s agent=%s debounce_ms=%s live=%s",
        context.root,
        context.name,
        int(float(debounce_value)),
        _short_fingerprint(context.live.fingerprint),
    )
    while True:
        if not await _wait_for_reload_or_stop(reload_signal, stop_signal):
            return
        logger.info("reload requested agent=%s", context.name)
        if await _debounce_reload(reload_signal, stop_signal, debounce_timeout):
            return
        try:
            prepared = load_prepared_state(context.root, context.name)
        except FileNotFoundError:
            logger.debug("reload skipped missing prepared state agent=%s", context.name)
            continue
        if context.live.fingerprint == prepared.fingerprint:
            logger.debug(
                "reload skipped unchanged fingerprint=%s agent=%s",
                _short_fingerprint(prepared.fingerprint),
                context.name,
            )
            continue
        enabled_loops = context.config.require("loops.enabled")
        if not isinstance(enabled_loops, tuple):
            raise TypeError("invalid config: loops.enabled")
        live = load_live_state(prepared, enabled_loops=cast(tuple[str, ...], enabled_loops))
        from ..up import load_runtime_tool_plugins

        tools = load_runtime_tool_plugins(
            toolang_root=context.root,
            agent_name=context.name,
            live=live,
            environ=context.model_environ,
        )
        logger.info(
            "reload applied agent=%s live=%s->%s",
            context.name,
            _short_fingerprint(context.live.fingerprint),
            _short_fingerprint(live.fingerprint),
        )
        context.live = live
        context.tools = tools


async def _wait_for_reload_or_stop(
    reload_signal: asyncio.Event, stop_signal: asyncio.Event
) -> bool:
    reload_task = asyncio.create_task(reload_signal.wait())
    stop_task = asyncio.create_task(stop_signal.wait())
    done, pending = await asyncio.wait(
        {reload_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return reload_task in done


async def _debounce_reload(
    reload_signal: asyncio.Event,
    stop_signal: asyncio.Event,
    debounce_timeout: float,
) -> bool:
    while True:
        reload_signal.clear()
        try:
            await asyncio.wait_for(stop_signal.wait(), timeout=debounce_timeout)
            return True
        except TimeoutError:
            if reload_signal.is_set():
                logger.debug("reload coalesced additional signals")
                continue
            return False


def _short_fingerprint(value: str) -> str:
    return value[:12]
