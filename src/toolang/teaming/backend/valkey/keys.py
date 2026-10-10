"""Teaming storage addresses; execution event keys retain their own namespace."""

import base64

from ...schemas import conversation_id, target

PREFIX = "too:teaming:v1"
TEAM = f"{PREFIX}:team"
PRESENCE = f"{PREFIX}:team:presence"
TEAM_EVENTS = f"{PREFIX}:team:events"
CONVOS = f"{PREFIX}:convos"
SCHEMA = f"{PREFIX}:convo:schema"
GC_ALLOCATOR = f"{PREFIX}:convo:id:gc"
STATS = f"{PREFIX}:convo:stats"
SYSTEM = f"{PREFIX}:convo:system"
ROSTER = f"{PREFIX}:roster"
BASE_KEYS = [
    SCHEMA,
    TEAM,
    PRESENCE,
    CONVOS,
    STATS,
    GC_ALLOCATOR,
    TEAM_EVENTS,
    SYSTEM,
    ROSTER,
]


def convo_key(conversation: str, suffix: str) -> str:
    return f"{PREFIX}:convo:{conversation_id(conversation)}:{suffix}"


def name_key(name: str | None) -> str:
    encoded = base64.urlsafe_b64encode((name or "").encode()).decode().rstrip("=")
    return f"{PREFIX}:convo:name:{encoded}"


# Execution publication has its own retention, recovery, and cursor metadata.
EVENT_META = f"{PREFIX}:events:meta"
EVENT_STREAM = f"{PREFIX}:events:stream"
EVENT_AGENTS = f"{PREFIX}:events:agents"


def generation_key(agent: str, generation: str) -> str:
    return f"{PREFIX}:events:agent:{target(agent, kind='agent').name}:{generation}"


def activity_key(agent: str) -> str:
    return f"{PREFIX}:activity:{target(agent, kind='agent').id}"
