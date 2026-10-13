"""Narrow transport contracts used by agent-side teaming work."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from .schemas import ConversationSummary

RENEW_SECONDS = 5
LEASE_SECONDS = 15
MESSAGE_RETENTION = 10000
TEAM_EVENT_RETENTION = 100000
STORAGE_BATCH_SIZE = 128
SNAPSHOT_RETRIES = 8
MAX_SAFE_INTEGER = 9007199254740990


class AgentTransport(Protocol):
    actor: str
    token: str

    async def register(self, owner: str, *, endpoint: str = "") -> None: ...
    async def renew(self) -> None: ...
    async def unregister(self) -> None: ...
    async def close(self) -> None: ...


class MessageReceiver(AgentTransport, Protocol):
    def session(self) -> AbstractContextManager[str]: ...
    async def contacts(
        self, *, include_preview: bool = False
    ) -> list[ConversationSummary]: ...
    async def talk_cursor(self, convo_id: str) -> str: ...
    async def advance_talk_cursor(self, convo_id: str, cursor: str) -> None: ...
    async def check_cursor(self, conversation: str, cursor: str) -> str | None: ...
    async def read(
        self, conversation: str, *, after: str = "0-0", count: int = 100
    ) -> list[tuple[str, dict[str, str]]]: ...
    async def send(
        self,
        destination: str,
        *,
        body: str,
        in_reply_to: str | None = None,
        handler_error: bool = False,
    ) -> dict[str, Any]: ...


class EventPublisher(Protocol):
    async def initialize(self) -> dict[str, str]: ...
    async def capture(
        self, agent: str | None = None
    ) -> tuple[dict[str, str], dict[str, Any], dict[str, str]]: ...
    async def commit(self, op: dict[str, Any]) -> str: ...
    async def stage(self, op: dict[str, Any]) -> None: ...
    async def abandon(self, agent: str, generation: str, *, token: str) -> None: ...
