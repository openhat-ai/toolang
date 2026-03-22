"""Agent and capability identity concepts."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
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


def agent_id(agent_uri: AgentUri) -> str:
    """Return the stable short-hash basis for one canonical agent URI."""

    return sha256(agent_uri.encode("utf-8")).hexdigest()


def agent_home_name(
    agent_uri: AgentUri,
    *,
    agent_name: str,
    kind: AgentKind,
) -> str:
    """Return the local home directory name used for one canonical agent URI."""

    if kind == "resident":
        return agent_uri.removeprefix("agent://").split("/", 1)[0]
    if kind == "roaming":
        return agent_name
    return f"{agent_name}-{agent_id(agent_uri)[:12]}"
