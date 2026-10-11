"""AgentServer vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DriverName = Literal["chat", "talk", "chore"]
ExecutionDrivers = frozenset[DriverName]
ALL_DRIVERS: ExecutionDrivers = frozenset(("chat", "talk", "chore"))


@dataclass(frozen=True, slots=True)
class AgentServerRef:
    """One AgentServer endpoint and its sandbox identity."""

    sandbox: str
    endpoint: str

    def __post_init__(self) -> None:
        sandbox = self.sandbox.strip()
        if not sandbox or sandbox != self.sandbox:
            raise ValueError("agent server requires a canonical sandbox")
        if not self.endpoint.strip():
            raise ValueError("agent server requires an endpoint")
        if self.endpoint != self.endpoint.strip():
            raise ValueError("agent server requires a canonical endpoint")
