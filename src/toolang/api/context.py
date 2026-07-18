"""Typed dependencies available to HTTP API routes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
import threading

from toolang.base.protocols.channel import AgentChannel
from toolang.config.runtime import RuntimeConfig
from toolang.execution.executor import Executor
from toolang.execution.store import RunStore
from toolang.plugin.config import ChannelBinding
from toolang.state.agent import AgentState


@dataclass(slots=True)
class ApiContext:
    """Dependencies owned by one FastAPI application."""

    root: Path
    name: str
    home: Path
    room: Path
    get_agent_state: Callable[[], AgentState]
    channel_bindings: Mapping[str, ChannelBinding]
    channel_plugins: Mapping[str, AgentChannel]
    executor: Executor
    store: RunStore
    config: RuntimeConfig
    enabled_components: tuple[str, ...]
    shutdown_signal: threading.Event | None = None
