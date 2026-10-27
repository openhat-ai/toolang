"""Immutable storage addresses for one host-selected Toolang root."""

import base64
from dataclasses import dataclass

from ...schemas import conversation_id, target


@dataclass(frozen=True)
class Keys:
    root_code: str

    @staticmethod
    def roots_pattern() -> str:
        return "toolang:*:v0:root"

    @property
    def prefix(self) -> str:
        return f"toolang:{self.root_code}:v0"

    @property
    def root(self) -> str:
        return f"{self.prefix}:root"

    @property
    def team_members(self) -> str:
        return f"{self.prefix}:team:members"

    @property
    def presence(self) -> str:
        return f"{self.prefix}:team:presence"

    @property
    def team_events(self) -> str:
        return f"{self.prefix}:team:events"

    @property
    def convos(self) -> str:
        return f"{self.prefix}:convos"

    @property
    def schema(self) -> str:
        return f"{self.prefix}:convo:schema"

    @property
    def gc_allocator(self) -> str:
        return f"{self.prefix}:convo:id:gc"

    @property
    def stats(self) -> str:
        return f"{self.prefix}:convo:stats"

    @property
    def system(self) -> str:
        return f"{self.prefix}:convo:system"

    @property
    def event_meta(self) -> str:
        return f"{self.prefix}:events:meta"

    @property
    def event_stream(self) -> str:
        return f"{self.prefix}:events:stream"

    @property
    def event_agents(self) -> str:
        return f"{self.prefix}:events:agents"

    @property
    def base(self) -> list[str]:
        return [
            self.schema,
            self.team_members,
            self.presence,
            self.convos,
            self.stats,
            self.gc_allocator,
            self.team_events,
            self.system,
        ]

    def convo_key(self, conversation: str, suffix: str) -> str:
        return f"{self.prefix}:convo:{conversation_id(conversation)}:{suffix}"

    def name_key(self, name: str | None) -> str:
        encoded = base64.urlsafe_b64encode((name or "").encode()).decode().rstrip("=")
        return f"{self.prefix}:convo:name:{encoded}"

    def generation_key(self, agent: str, generation: str) -> str:
        return f"{self.prefix}:events:agent:{target(agent, kind='agent').name}:{generation}"

    def talk_key(self, agent: str) -> str:
        return f"{self.prefix}:agent:{target(agent, kind='agent').name}:talk"
