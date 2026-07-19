"""Poll loop producer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import json
import logging
from pathlib import Path

from toolang.base.protocols.channel import AgentChannel
from toolang.base.types.channel import ChannelContext, ChannelState, InboundDelivery
from toolang.base.utils.channels import bind_delivery
from toolang.execution.executor import Executor
from toolang.execution.records import RunRecord
from toolang.execution.reply import build_channel_reply_sink
from toolang.execution.request import RunRequest
from toolang.plugin.config import ChannelBinding
from toolang.state.state import AgentState

DEFAULT_INTERVAL_MS = 300.0
logger = logging.getLogger("toolang.poll")


def spawn(
    *,
    name: str,
    home: Path,
    bindings: Mapping[str, ChannelBinding],
    plugins: Mapping[str, AgentChannel],
    executor: Executor,
    get_agent_state: Callable[[], AgentState],
    interval_ms: float,
    stop_signal: asyncio.Event,
) -> asyncio.Task[None]:
    """Spawn the poll loop in one background task."""

    return asyncio.create_task(
        run(
            name=name,
            home=home,
            bindings=bindings,
            plugins=plugins,
            executor=executor,
            get_agent_state=get_agent_state,
            interval_ms=interval_ms,
            stop_signal=stop_signal,
        )
    )


async def run(
    *,
    name: str,
    home: Path,
    bindings: Mapping[str, ChannelBinding],
    plugins: Mapping[str, AgentChannel],
    executor: Executor,
    get_agent_state: Callable[[], AgentState],
    interval_ms: float,
    stop_signal: asyncio.Event,
) -> None:
    """Start poll runs until the runtime stops."""
    interval_timeout = interval_ms / 1000
    logger.debug(
        "poll.started root=%s agent=%s interval_ms=%s bindings=%s",
        home.parent.parent,
        name,
        int(interval_ms),
        ",".join(sorted(bindings)) or "-",
    )
    while True:
        for binding_name in sorted(bindings):
            await _poll_binding(
                name=name,
                home=home,
                binding_name=binding_name,
                plugins=plugins,
                executor=executor,
                get_agent_state=get_agent_state,
            )
        try:
            await asyncio.wait_for(stop_signal.wait(), timeout=interval_timeout)
        except TimeoutError:
            continue
        else:
            return


async def _poll_binding(
    *,
    name: str,
    home: Path,
    binding_name: str,
    plugins: Mapping[str, AgentChannel],
    executor: Executor,
    get_agent_state: Callable[[], AgentState],
) -> None:
    plugin = plugins.get(binding_name)
    if plugin is None:
        return
    bound_context = channel_context(home, binding_name)
    state_path = bound_context.room / "state.json"
    state = _load_state(state_path)
    try:
        result = await asyncio.to_thread(plugin.poll, state, bound_context)
    except Exception as exc:
        logger.warning(
            "poll failed agent=%s binding=%s error=%s",
            name,
            binding_name,
            exc,
        )
        return
    _write_state(state_path, result.next_state)
    if not result.deliveries:
        return
    logger.debug(
        "poll.received agent=%s binding=%s deliveries=%s cursor=%s",
        name,
        binding_name,
        len(result.deliveries),
        result.next_state.cursor or "-",
    )
    for delivery in result.deliveries:
        start_delivery(
            executor=executor,
            get_agent_state=get_agent_state,
            plugins=plugins,
            home=home,
            group="chat",
            binding_name=binding_name,
            delivery=delivery,
        )


def _load_state(path: Path) -> ChannelState:
    if not path.is_file():
        return ChannelState()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return ChannelState()
    return ChannelState.from_data(payload)


def channel_context(home: Path, binding_name: str) -> ChannelContext:
    return ChannelContext(
        home=home,
        room=home / ".runtime" / "channels" / binding_name,
    )


def start_delivery(
    *,
    executor: Executor,
    get_agent_state: Callable[[], AgentState],
    plugins: Mapping[str, AgentChannel],
    home: Path,
    group: str,
    binding_name: str,
    delivery: InboundDelivery,
) -> asyncio.Task[RunRecord]:
    bound = bind_delivery(binding_name, delivery)
    metadata = {**bound.meta, "channel": binding_name, "sender": bound.sender}
    return executor.start(
        RunRequest(
            group=group,
            origin=bound.origin,
            thread_id=bound.thread_id,
            input=bound.text,
            metadata=metadata,
        ),
        get_agent_state(),
        reply=build_channel_reply_sink(
            plugin=plugins.get(binding_name),
            channel_context=channel_context(home, binding_name),
            binding_name=binding_name,
            target=bound.reply_target,
        ),
    )


def _write_state(path: Path, state: ChannelState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.to_data(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
