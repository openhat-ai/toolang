"""Agent hosting and observation vocabulary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit

from toolang.common.layout import AgentPlacement
from toolang.execution.types import DriverName

ExecutionDrivers = frozenset[DriverName]
ALL_DRIVERS: ExecutionDrivers = frozenset(("chat", "talk", "chore"))
ServerLifetime = Literal["persistent", "foreground", "automatic"]


@dataclass(frozen=True, slots=True)
class PlacementPolicy:
    """The execution defaults and reuse boundary that vary by placement."""

    reuse_executor: bool
    hosted_drivers: ExecutionDrivers
    client_drivers: ExecutionDrivers
    client_lifetime: ServerLifetime


class TopPeriod(StrEnum):
    hour = "1h"
    four_hours = "4h"
    eight_hours = "8h"

    @property
    def seconds(self) -> int:
        return int(self.value[:-1]) * 3600


class TopRecent(StrEnum):
    minute = "1m"
    five_minutes = "5m"
    fifteen_minutes = "15m"

    @property
    def seconds(self) -> int:
        return int(self.value[:-1]) * 60


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
class AgentStatus:
    """One runtime observation bound to its authoritative instance entry."""

    name: str
    status: str
    endpoint: str | None
    api_url: str | None
    webui_url: str | None
    sandbox: str | None
    message: str | None = None
    entry: dict | None = field(default=None, repr=False)
    alive: bool | None = None

    @property
    def port(self) -> int | None:
        """Return the runtime endpoint port when one is known."""

        if self.endpoint is None:
            return None
        try:
            return urlsplit(self.endpoint).port
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class RuntimeInstance:
    """One agent in the runtime inventory, including inactive residents."""

    name: str
    placement: AgentPlacement
    runtime_instance: str | None
    identity: str
    state: str
    drivers: tuple[str, ...] | None
    observed_state: str | None = None
    diagnostic: str | None = None

    @property
    def needs_live_observation(self) -> bool:
        """Whether active roots cannot be ruled out from the observed state."""
        return self.state not in {"not running", "stopped", "failed"}

    @property
    def label(self) -> str:
        """Return a stable display name that distinguishes placements and instances."""
        if self.placement == "resident":
            return self.name
        if self.identity.startswith(f"{self.name}@"):
            return self.identity
        # Activity wire identities remain opaque for existing API consumers.
        marker = "v" if self.placement == "visiting" else "r"
        value = f"{self.name}@{marker}{self.identity[:8]}"
        return value + (f"#{self.runtime_instance}" if self.runtime_instance else "")


@dataclass(frozen=True, slots=True)
class RuntimeInstanceSnapshot:
    """A point-in-time roster and discovery diagnostics for one Toolang root."""

    instances: tuple[RuntimeInstance, ...]
    diagnostics: tuple[str, ...] = ()
    membership_complete: bool = True

    @property
    def partial(self) -> bool:
        return (
            not self.membership_complete
            or bool(self.diagnostics)
            or any(item.diagnostic is not None for item in self.instances)
        )
