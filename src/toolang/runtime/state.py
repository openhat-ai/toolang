"""Live runtime state for one running agent process."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import threading

from toolang.agent.prepared import PreparedAgent
from toolang.bus.db import BusStore
from toolang.channels import ChannelPlugin
from toolang.concepts.layout import AgentRoom
from toolang.errors import ToolangError

from .execution_store import ExecutionStore


@dataclass(slots=True)
class RuntimeState:
    """Mutable in-memory runtime state for one active process."""

    prepared: PreparedAgent
    room: AgentRoom | None = None
    bus: BusStore | None = None
    execution: ExecutionStore | None = None
    channel_plugins: dict[str, ChannelPlugin] = field(default_factory=dict)
    pulse_pending: set[str] = field(default_factory=set)
    pulse_pending_lock: threading.Lock = field(default_factory=threading.Lock)
    pulse_state_lock: threading.Lock = field(default_factory=threading.Lock)
    live_lock: threading.Lock = field(default_factory=threading.Lock)
    stop_event: threading.Event | None = None
    threads: list[threading.Thread] = field(default_factory=list)
    started: bool = False

    def current_prepared(self) -> PreparedAgent:
        """Return the current live prepared snapshot."""

        with self.live_lock:
            return self.prepared

    def replace_prepared(self, prepared: PreparedAgent) -> PreparedAgent:
        """Replace the current live prepared snapshot."""

        with self.live_lock:
            self.prepared = prepared
            return prepared

    def require_room(self) -> AgentRoom:
        """Return the current agent room or raise."""

        if self.room is None:
            raise ToolangError("Runtime process has not been started.")
        return self.room

    def require_bus(self) -> BusStore:
        """Return the current bus store or raise."""

        if self.bus is None:
            raise ToolangError("Runtime process has not been started.")
        return self.bus

    def require_execution(self) -> ExecutionStore:
        """Return the current execution store or raise."""

        if self.execution is None:
            raise ToolangError("Runtime process has not been started.")
        return self.execution

    def require_stop_event(self) -> threading.Event:
        """Return the current stop event or raise."""

        if self.stop_event is None:
            raise ToolangError("Runtime process has not been started.")
        return self.stop_event

    def pulse_pending_keys(self) -> set[str]:
        """Return the current set of pulse-pending keys."""

        with self.pulse_pending_lock:
            return set(self.pulse_pending)

    def mark_pulse_pending(self, pending_key: str) -> bool:
        """Mark one pulse submission as pending."""

        with self.pulse_pending_lock:
            if pending_key in self.pulse_pending:
                return False
            self.pulse_pending.add(pending_key)
            return True

    def clear_pulse_pending(self, pending_key: str) -> None:
        """Clear one pulse-pending key."""

        with self.pulse_pending_lock:
            self.pulse_pending.discard(pending_key)

    def load_pulse_state(self, state_path: Path):
        """Load one persisted pulse state under the pulse-state lock."""

        from toolang.concepts.persisted import PulseState

        with self.pulse_state_lock:
            if state_path.exists():
                return PulseState.load(state_path)
            return PulseState()

    def save_pulse_state(self, state_path: Path, state) -> None:
        """Save one persisted pulse state under the pulse-state lock."""

        with self.pulse_state_lock:
            state.save(state_path)
