"""Shared messaging rules for hosted agents, tools, and human clients."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from uuid import uuid4
import json

from .ids import dm_id

from .backend import Backend
from .backend.factory import create_backend
from .discovery import host_token as host_token
from .types import RENEW_SECONDS as RENEW_SECONDS
from .config import BackendConfig
from .errors import ConversationAccessDenied, LeaseLost, MessagingError
from .schemas import (
    Conversation,
    TeamMember,
    Targets,
    ConversationSummary,
    MessagePreview,
    Message,
    direct_pair,
    Resolution,
    conversation_id,
    identifier,
    participant,
    stream_id,
    target,
)

__all__ = ["MessagingClient", "RENEW_SECONDS", "host_token"]


class MessagingClient:
    def __init__(
        self,
        config: BackendConfig,
        *,
        actor: str,
        backend: Backend | None = None,
        token: str | None = None,
    ):
        who = participant(actor)
        self.config, self.actor = config, actor
        self.token = token if token is not None else host_token()
        if who.kind == "agent" and (not isinstance(self.token, str) or not self.token):
            raise MessagingError("Agent lease token must be nonempty text")
        self.backend: Backend = (
            backend if backend is not None else create_backend(config)
        )

    @contextmanager
    def session(self):
        yield self.config.identity

    async def __aenter__(self) -> MessagingClient:
        try:
            await self.check_backend()
            if target(self.actor).kind == "human":
                await self.register_human()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self.backend.close()

    async def check_backend(self) -> None:
        await self.backend.ping()
        await self.backend.initialize()

    async def register_human(self) -> None:
        """Ensure the human and system membership exist without changing custom conversations."""
        target(self.actor, kind="human")
        await self.backend.register(self.actor)

    @property
    def _lease(self) -> str:
        return self.token if target(self.actor).kind == "agent" else ""

    async def register(self, owner: str, *, endpoint: str = "") -> None:
        target(self.actor, kind="agent")
        target(owner, kind="human")
        await self.backend.register(
            owner, agent=self.actor, token=self.token, endpoint=endpoint
        )

    async def renew(self) -> None:
        if not await self.backend.renew_lease(self.actor, self.token):
            raise LeaseLost(f"Agent lease lost for {self.actor}")

    async def unregister(self) -> None:
        await self.backend.release_lease(self.actor, self.token)

    async def agents(self) -> dict[str, str]:
        return {
            name: info["owner"]
            for name, info in (await self.backend.participants()).items()
            if target(name).kind == "agent"
        }

    async def team(self) -> list[TeamMember]:
        return await self.backend.team()

    async def conversation(self, conversation: str) -> Conversation:
        info = await self.backend.conversation(conversation, self.actor)
        if info is None:
            raise MessagingError(f"Unknown conversation: {conversation}")
        return info

    async def resolve(
        self, value: str, *, kind: str | None = None, create: bool = False
    ) -> Resolution:
        if kind == "name":
            matches = []
            for ref in await self.backend.named(value):
                try:
                    info = await self.conversation(ref)
                except ConversationAccessDenied:
                    continue
                if info.name == value:
                    matches.append(info)
            if not matches:
                raise MessagingError(f"Unknown conversation name: {value}")
            if len(matches) != 1:
                raise MessagingError(
                    "Ambiguous conversation name: "
                    + json.dumps(
                        [
                            dict(id=c.id, kind=c.kind, participants=c.participants)
                            for c in matches
                        ]
                    )
                )
            info = matches[0]
            return Resolution(info.id, info.participants, True)
        if kind is not None:
            raise MessagingError("Unsupported lookup kind")
        try:
            canonical = conversation_id(value)
        except MessagingError:
            canonical = None
        if canonical:
            info = await self.conversation(canonical)
            return Resolution(info.id, info.participants, True)
        if "," in value:
            names = value.split(",")
            if len(names) != 2:
                raise MessagingError("A DM requires exactly two distinct participants")
            pair = tuple(f"agent:{identifier(name)}" for name in names)
        else:
            selected = (
                participant(value).id if ":" in value else f"agent:{identifier(value)}"
            )
            pair = (self.actor, selected)
        a, b = pair
        direct_pair(a, b)
        for member in pair:
            if not await self.backend.known_participant(member):
                raise MessagingError(f"Unknown participant: {member}")
        ref = dm_id(a, b)
        info = await self.backend.conversation(ref, self.actor, pair=pair)
        if not info and self.actor not in pair:
            raise MessagingError("No conversation exists between these participants")
        if create and not info:
            await self.backend.create_dm(self.actor, (a, b), token=self._lease)
            info = await self.conversation(ref)
        return Resolution(ref, tuple(sorted(pair)), info is not None)

    async def create_conversation(
        self, name: str | None = None, *, participants: list[str] | None = None
    ) -> Conversation:
        if participants is None:
            ref = await self.backend.create_gc(self.actor, token=self._lease, name=name)
        else:
            if len(participants) != 2:
                raise MessagingError("A DM requires exactly two distinct participants")
            ref = await self.backend.create_dm(
                self.actor,
                (participants[0], participants[1]),
                token=self._lease,
                name=name,
            )
        return await self.conversation(ref)

    async def rename_conversation(
        self, conversation: str, name: str | None, *, revision: int
    ) -> Conversation:
        await self.backend.rename(
            conversation, self.actor, token=self._lease, name=name, revision=revision
        )
        return await self.conversation(conversation)

    async def join_conversation(self, conversation: str) -> dict[str, Any]:
        await self.backend.member(
            conversation, self.actor, token=self._lease, join=True
        )
        return {
            "conversation": conversation,
            "participants": list((await self.conversation(conversation)).participants),
        }

    async def leave_conversation(self, conversation: str) -> dict[str, Any]:
        await self.backend.member(
            conversation, self.actor, token=self._lease, join=False
        )
        return {"conversation": conversation}

    async def contacts(
        self, *, include_preview: bool = False
    ) -> list[ConversationSummary]:
        result = []
        for info, latest in await self.backend.contacts(self.actor):
            ref = info.id
            item: ConversationSummary = dict(
                conversation=ref,
                name=info.name,
                kind=info.kind,
                revision=info.revision,
                participants=list(info.participants),
                latest=latest[0][0] if latest else None,
            )
            if include_preview:
                preview: MessagePreview | None = None
                if latest:
                    try:
                        fields = latest[0][1]
                        message = Message.decode(fields["data"])
                        preview = {"sender": message.sender, "body": message.body[:160]}
                    except (MessagingError, KeyError):
                        pass
                item["preview"] = preview
            result.append(item)
        return result

    async def targets(self) -> Targets:
        return {
            "participants": await self.team(),
            "conversations": await self.contacts(),
        }

    async def statistics(
        self, conversation: str | None = None
    ) -> dict[str, int | str | None]:
        return await self.backend.statistics(self.actor, conversation)

    async def send(
        self,
        destination: str,
        *,
        body: str,
        in_reply_to: str | None = None,
        run: str | None = None,
        thread: str | None = None,
        message_id: str | None = None,
        participants: list[str] | None = None,
    ) -> dict[str, Any]:
        message = Message(
            message_id if message_id is not None else str(uuid4()),
            self.actor,
            body,
            in_reply_to,
            {"thread": thread, "run": run} if self._lease else None,
        )
        if participants is not None:
            if len(participants) != 2 or dm_id(*participants) != conversation_id(
                destination
            ):
                raise MessagingError("DM pair does not match conversation ID")
            ref, pair = destination, tuple(participants)
        else:
            resolved = await self.resolve(destination)
            ref, pair = (
                resolved.conversation,
                resolved.participants
                if resolved.conversation.startswith("dm_")
                else (),
            )
        sid = await self.backend.append(
            ref, self.actor, message.encode(), token=self._lease, pair=pair
        )
        return {"conversation": ref, "stream_id": sid, "message": message.data()}

    async def read(
        self, conversation: str, *, after: str = "0-0", count: int = 100
    ) -> list[tuple[str, dict[str, str]]]:
        stream_id(after)
        if not 1 <= count <= 1000:
            raise MessagingError("Read count must be between 1 and 1000")
        await self.conversation(conversation)
        return await self.backend.messages(
            conversation, self.actor, start=f"({after}", finish="+", count=count
        )

    async def history(
        self, conversation: str, *, count: int = 200
    ) -> list[tuple[str, dict[str, str]]]:
        if not 1 <= count <= 1000:
            raise MessagingError("History count must be between 1 and 1000")
        await self.conversation(conversation)
        return list(
            reversed(
                await self.backend.messages(
                    conversation,
                    self.actor,
                    start="+",
                    finish="-",
                    count=count,
                    reverse=True,
                )
            )
        )

    async def check_cursor(self, conversation: str, cursor: str) -> str | None:
        stream_id(cursor)
        await self.conversation(conversation)
        if cursor == "0-0":
            return None
        first = await self.backend.messages(
            conversation, self.actor, start="-", finish="+", count=1
        )
        if not first:
            return "Conversation history is missing"
        last = await self.history(conversation, count=1)
        if stream_id(cursor) > stream_id(last[0][0]):
            raise MessagingError(
                "Stream precedes saved cursor; explicitly reset the local checkpoint"
            )
        if stream_id(cursor) < stream_id(first[0][0]):
            return "Earlier conversation history is no longer retained"
        return None
