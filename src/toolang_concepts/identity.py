"""Agent and capability identity concepts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

AgentKind = Literal["resident", "roaming", "visiting"]
AgentSelector: TypeAlias = str
AgentUri: TypeAlias = str


@dataclass(frozen=True, slots=True)
class AgentRef:
    """A canonical agent identity paired with local placement details."""

    selector: AgentSelector
    kind: AgentKind
    uri: AgentUri
    id: str
    root: Path
    home: Path
    name: str
    source: Path
