"""Runtime resources shared by HTTP and trigger components."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from toolang.base.types.channel import ChannelContext, InboundDelivery
from toolang.base.utils.channels import bind_delivery
from starlette.datastructures import State

from toolang.agent import local as agents
from ..execution.reply import build_channel_reply_sink
from ..execution.records import RunRecord
from ..execution.request import RunRequest
from .features import RUNNER_COMPONENTS, component_group

ComponentState = State

RUN_COMPONENTS = frozenset(component_group(RUNNER_COMPONENTS, "runner")) | {
    "pulse",
    "poll",
}


class RuntimeConfig:
    """Mutable process configuration resolved before component startup."""

    def __init__(self, values: Mapping[str, object] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, key: str, default: object | None = None) -> object | None:
        return self._values.get(key, default)

    def require(self, key: str) -> object:
        if key not in self._values:
            raise KeyError(f"missing config: {key}")
        return self._values[key]

    def set(self, key: str, value: object) -> None:
        self._values[key] = value

    def snapshot(self) -> dict[str, object]:
        return dict(self._values)


def channel_context(state: ComponentState, binding_name: str) -> ChannelContext:
    return ChannelContext(
        home=state.home,
        room=agents.channel_room(state.root, state.name, binding_name),
    )


def start_delivery(
    state: ComponentState,
    component_name: str,
    binding_name: str,
    delivery: InboundDelivery,
) -> asyncio.Task[RunRecord]:
    if component_name not in RUN_COMPONENTS:
        raise ValueError(f"component does not produce runs: run.{component_name}")
    bound = bind_delivery(binding_name, delivery)
    metadata = {**bound.meta, "channel": binding_name, "sender": bound.sender}
    return state.executor.start(
        RunRequest(
            group=component_name,
            origin=bound.origin,
            thread_id=bound.thread_id,
            input=bound.text,
            metadata=metadata,
        ),
        state.get_agent_state(),
        reply=build_channel_reply_sink(
            plugin=state.channel_plugins.get(binding_name),
            channel_context=channel_context(state, binding_name),
            binding_name=binding_name,
            target=bound.reply_target,
        ),
    )
