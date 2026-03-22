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

    raw: AgentSelector
    agent_kind: AgentKind
    agent_uri: AgentUri
    agent_id: str
    toolang_root: Path
    agent_home: Path
    agent_name: str
    source_path: Path
