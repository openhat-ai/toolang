"""AgentServer vocabulary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from toolang.common.layout import AgentPlacement
from toolang.execution.types import DriverName

ExecutionDrivers = frozenset[DriverName]
ALL_DRIVERS: ExecutionDrivers = frozenset(("chat", "talk", "chore"))
ServerLifetime = Literal["persistent", "foreground", "automatic"]


@dataclass(frozen=True, slots=True)
class AgentServerRef:
    """One AgentServer endpoint and its sandbox identity."""

    sandbox: str
    endpoint: str
    launch_id: str | None = None
    lifetime: ServerLifetime | None = None
    client_id: str | None = None
    check_retention: Callable[[], None] | None = field(
        default=None, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        sandbox = self.sandbox.strip()
        if not sandbox or sandbox != self.sandbox:
            raise ValueError("agent server requires a canonical sandbox")
        if not self.endpoint.strip():
            raise ValueError("agent server requires an endpoint")
        if self.endpoint != self.endpoint.strip():
            raise ValueError("agent server requires a canonical endpoint")


@dataclass(frozen=True, slots=True)
class RuntimeInstance:
    """One installed or registered runtime visible to the Hub snapshot."""

    name: str
    placement: AgentPlacement
    runtime_instance: str | None
    identity: str
    state: str
    drivers: tuple[str, ...] | None
    observed_state: str | None = None
    diagnostic: str | None = None

    @property
    def label(self) -> str:
        """Return a stable display name that distinguishes placements and instances."""
        if self.placement == "resident":
            return (
                self.name
                if self.runtime_instance is None
                else f"{self.name}#{self.runtime_instance}"
            )
        marker = "v" if self.placement == "visiting" else "r"
        value = f"{self.name}@{marker}{self.identity[:8]}"
        if self.runtime_instance is not None:
            value += f"#{self.runtime_instance}"
        return value


@dataclass(frozen=True, slots=True)
class RuntimeInstanceSnapshot:
    """A point-in-time roster and discovery diagnostics for one Toolang root."""

    instances: tuple[RuntimeInstance, ...]
    diagnostics: tuple[str, ...] = ()

    @property
    def partial(self) -> bool:
        return bool(self.diagnostics) or any(
            item.diagnostic is not None for item in self.instances
        )
