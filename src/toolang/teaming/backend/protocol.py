"""Driver-independent storage interfaces used by teaming services.

The backend owns one connection lifecycle. Its activity and execution-event stores
share that lifecycle; they are not independently opened or closed. Implementations
normalize driver replies and enforce atomic lease/membership checks on writes.
No keys, commands, scripts, or concrete driver types belong in these interfaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from ..types import EventPublisher

if TYPE_CHECKING:
    from toolang.execution.activity import ActivityQuery
    from toolang.execution.schemas import ActivitySnapshot
    from ..events import HubCursor
    from ..schemas import Conversation, TeamMember


class ActivityBackend(Protocol):
    """Absolute activity snapshots, fenced by the current agent lease."""

    async def lease(self, agent: str) -> dict[str, str]:
        """Read the current live lease, or an empty mapping when offline."""
        ...

    async def save(
        self, agent: str, token: str, pages: list[ActivitySnapshot]
    ) -> bool: ...
    async def cached(
        self, agent: str, query: ActivityQuery
    ) -> list[ActivitySnapshot]: ...


class EventBackend(EventPublisher, Protocol):
    """Execution-event publication and replay, separate from team-change events."""

    async def projection(
        self, agent: str, generation: str
    ) -> dict[str, dict[str, Any]]: ...
    async def online_token(self, agent: str) -> str | None: ...
    async def read(
        self, cursor: HubCursor, until: str = "+"
    ) -> tuple[str, list[tuple[str, dict[str, str]]]]: ...
    async def wait(self, after: str) -> None: ...


class Backend(Protocol):
    """Shared team, roster, presence, conversation and message storage.

    Team is the member directory; roster is optional root/discovery metadata for
    agents in that directory. Presence deadlines remain the online authority.
    Registration and presence expiry publish team events atomically. Conversation
    mutations enforce actor membership and lease fencing in the same transaction.
    Reads never renew leases or reconcile expired presence.
    """

    @property
    def events(self) -> EventBackend: ...

    @property
    def activity(self) -> ActivityBackend: ...

    # Lifetime

    async def close(self) -> None: ...

    async def ping(self) -> None: ...

    async def initialize(self) -> None: ...

    # Team directory and optional discovery metadata

    async def team(self) -> list[TeamMember]: ...

    async def participants(self) -> dict[str, dict[str, Any]]: ...

    async def known_participant(self, member: str) -> bool: ...

    async def roster(self) -> dict[str, dict]: ...

    async def reconcile_roster(
        self, agent: str, owner: str, previous: dict | None, updated: dict | None
    ) -> bool:
        """Compare and replace discovery metadata; return False on a conflict."""
        ...

    # Lease ownership and presence maintenance

    async def register(
        self,
        human: str,
        *,
        agent: str | None = None,
        token: str = "",
        endpoint: str = "",
        root: str = "",
    ) -> None: ...

    async def renew_lease(self, agent: str, token: str) -> bool:
        """Extend the current lease by LEASE_SECONDS; False means lease lost."""
        ...

    async def release_lease(self, agent: str, token: str) -> bool:
        """Release only the matching lease; False means it is no longer current."""
        ...

    async def online(self, agent: str) -> bool: ...

    async def due_presence(self) -> list[str]: ...

    async def expire_presence(self, agent: str) -> bool:
        """Expire and emit offline atomically only if still due; report a change."""
        ...

    # Conversations and messages

    async def conversation(
        self, conversation: str, actor: str, *, pair: tuple[str, ...] = ()
    ) -> Conversation | None: ...

    async def contacts(
        self, actor: str
    ) -> list[tuple[Conversation, list[tuple[str, dict[str, str]]]]]: ...

    async def named(self, name: str) -> list[str]: ...

    async def create_dm(
        self,
        actor: str,
        pair: tuple[str, str],
        *,
        token: str = "",
        name: str | None = None,
    ) -> str: ...

    async def create_gc(
        self, actor: str, *, token: str = "", name: str | None = None
    ) -> str:
        """Allocate a new GC; retries may create another conversation."""
        ...

    async def member(
        self, conversation: str, actor: str, *, token: str, join: bool
    ) -> None: ...

    async def rename(
        self,
        conversation: str,
        actor: str,
        *,
        token: str,
        name: str | None,
        revision: int,
    ) -> None: ...

    async def append(
        self,
        conversation: str,
        actor: str,
        data: str,
        *,
        token: str,
        pair: tuple[str, ...] = (),
    ) -> str: ...

    async def messages(
        self,
        conversation: str,
        actor: str,
        *,
        start: str,
        finish: str,
        count: int,
        reverse: bool = False,
    ) -> list[tuple[str, dict[str, str]]]: ...

    async def statistics(
        self, actor: str, conversation: str | None = None
    ) -> dict[str, int | str | None]: ...

    # Team-change feed (independent of execution events)

    async def team_event_position(self) -> tuple[str, str]: ...

    async def team_event_replay(
        self, epoch: str, after: str, *, actor: str | None = None, token: str = ""
    ) -> list[tuple[str, dict]] | None:
        """Return changes after the cursor, or None when a resync is required."""
        ...

    async def wait_team_events(self, after: str) -> None: ...
