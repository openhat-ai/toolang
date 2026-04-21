"""Poll loop producer."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from toolang.base.types.channel import ChannelState

if TYPE_CHECKING:
    from ..up import UptimeContext

DEFAULT_INTERVAL_MS = 300.0
logger = logging.getLogger("toolang.loop.poll")


def spawn(
    context: UptimeContext,
    *,
    stop_signal: asyncio.Event,
) -> asyncio.Task[None]:
    """Spawn the poll loop in one background task."""

    return asyncio.create_task(run(context, stop_signal=stop_signal))


async def run(
    context: UptimeContext,
    *,
    stop_signal: asyncio.Event,
) -> None:
    """Enqueue poll runs until the runtime stops."""
    interval_value = context.config.require("loops.poll.interval_ms")
    if not isinstance(interval_value, int | float):
        raise TypeError("invalid config: loops.poll.interval_ms")
    interval_timeout = float(interval_value) / 1000
    logger.debug(
        "poll loop started root=%s agent=%s interval_ms=%s bindings=%s",
        context.root,
        context.name,
        int(float(interval_value)),
        ",".join(sorted(context.channel_bindings)) or "-",
    )
    while True:
        for binding_name in sorted(context.channel_bindings):
            await _poll_binding(context, binding_name)
        try:
            await asyncio.wait_for(stop_signal.wait(), timeout=interval_timeout)
        except TimeoutError:
            continue
        else:
            return


async def _poll_binding(context: UptimeContext, binding_name: str) -> None:
    plugin = context.channel_plugins.get(binding_name)
    if plugin is None:
        return
    channel_context = context.channel_context(binding_name)
    state_path = channel_context.room / "state.json"
    state = _load_state(state_path)
    try:
        result = await asyncio.to_thread(plugin.poll, state, channel_context)
    except Exception as exc:
        logger.warning(
            "poll failed agent=%s binding=%s error=%s",
            context.name,
            binding_name,
            exc,
        )
        return
    _write_state(state_path, result.next_state)
    if not result.deliveries:
        return
    logger.info(
        "poll received agent=%s binding=%s deliveries=%s cursor=%s",
        context.name,
        binding_name,
        len(result.deliveries),
        result.next_state.cursor or "-",
    )
    for delivery in result.deliveries:
        context.enqueue_delivery("poll", binding_name, delivery)


def _load_state(path: Path) -> ChannelState:
    if not path.is_file():
        return ChannelState()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return ChannelState()
    return ChannelState.from_data(payload)


def _write_state(path: Path, state: ChannelState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.to_data(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
