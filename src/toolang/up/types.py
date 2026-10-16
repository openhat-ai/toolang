"""AgentServer vocabulary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

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
