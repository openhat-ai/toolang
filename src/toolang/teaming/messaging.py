"""Shared messaging rules for hosted agents, tools, and human clients."""

from __future__ import annotations

from functools import lru_cache
import os
from typing import Any
from uuid import uuid4

from .backend import Backend, LEASE_SECONDS, RENEW_SECONDS
from .config import BackendConfig
from .errors import MessagingError
from .schemas import (
    Conversation,
    Message,
    direct_pair,
    identifier,
    participant,
    stream_id,
    target,
)

__all__ = ["MessagingClient", "RENEW_SECONDS", "host_token"]


@lru_cache
def _process_token(pid: int) -> str:
    return str(uuid4())


def host_token() -> str:
    """Share one lease identity across this process's loop and tool clients."""
    return _process_token(os.getpid())


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
        self._backend = backend or Backend(config)

    async def __aenter__(self) -> MessagingClient:
        if target(self.actor).kind == "human":
            try:
                await self._backend.register(self.actor)
            except BaseException:
                await self.close()
                raise
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._backend.close()

    async def check_backend(self) -> None:
        await self._backend.ping()

    @property
    def _lease(self) -> str:
        return self.token if target(self.actor).kind == "agent" else ""

    async def register(self, owner: str, *, endpoint: str = "") -> None:
        target(self.actor, kind="agent")
        target(owner, kind="human")
        await self._backend.register(
            owner, agent=self.actor, token=self.token, endpoint=endpoint
        )

    async def renew(self) -> None:
        if not await self._backend.lease(self.actor, self.token, LEASE_SECONDS):
            raise MessagingError(f"Agent lease lost for {self.actor}")

    async def unregister(self) -> None:
        await self._backend.lease(self.actor, self.token, 0)

    async def agents(self) -> dict[str, str]:
        return {
            name: info["owner"]
            for name, info in (await self._backend.participants()).items()
            if target(name).kind == "agent"
        }

    async def conversation(self, group: str) -> Conversation:
        target(group, kind="group")
        info = (await self._backend.groups()).get(group)
        if info is None:
            raise MessagingError(f"Unknown group: {group}")
        members = await self._backend.members(group)
        if self._lease and self.actor not in members:
            raise MessagingError("Agent is not a member of this group")
        return Conversation(
            group, info["kind"], members, info["display_name"], info["system"]
        )

    async def resolve(self, value: str, *, kind: str | None = None) -> str:
        participants = await self._backend.participants()
        groups = await self._backend.groups()
        if ":" in value:
            selected = target(value)
            if kind and selected.kind != ("agent" if kind == "dm" else kind):
                raise MessagingError("Target conflicts with the requested type")
        elif value == "all" and kind is None:
            selected = target("group:all")
        elif kind is not None:
            selected = target(
                f"{'agent' if kind == 'dm' else kind}:{identifier(value)}"
            )
        else:
            identifier(value)
            matches = [
                ref for ref in (*participants, *groups) if target(ref).name == value
            ]
            if len(matches) > 1:
                raise MessagingError("Ambiguous target; use agent:, human:, or group:")
            if not matches:
                raise MessagingError(f"Unknown target: {value}")
            selected = target(matches[0])
        if selected.kind == "group":
            await self.conversation(selected.id)
            return selected.id
        if selected.id not in participants:
            raise MessagingError(f"Unknown participant: {selected.id}")
        direct_pair(self.actor, selected.id)
        for _ in range(8):
            existing = await self._backend.direct(self.actor, selected.id)
            if existing:
                return existing
            group = f"group:{uuid4()}"
            if await self._backend.create(
                group, self.actor, token=self._lease, other=selected.id
            ):
                return group
        raise MessagingError("Could not allocate a direct conversation")

    async def create_group(self, name: str) -> dict[str, Any]:
        group = f"group:{identifier(name)}"
        if group == "group:all":
            raise MessagingError("The all group is reserved")
        if not await self._backend.create(group, self.actor, token=self._lease):
            raise MessagingError(f"Group already exists: {group}")
        return {"group": group, "members": [self.actor]}

    async def join_group(self, group: str) -> dict[str, Any]:
        target(group, kind="group")
        await self._backend.member(group, self.actor, token=self._lease, join=True)
        return {"group": group, "members": list(await self._backend.members(group))}

    async def leave_group(self, group: str) -> dict[str, Any]:
        target(group, kind="group")
        await self._backend.member(group, self.actor, token=self._lease, join=False)
        return {"group": group, "members": list(await self._backend.members(group))}

    async def contacts(self, *, include_preview: bool = False) -> list[dict[str, Any]]:
        agents = await self.agents()
        online = {agent for agent in agents if await self._backend.online(agent)}
        result = []
        for group, info in (await self._backend.groups()).items():
            members = await self._backend.members(group)
            if self._lease and self.actor not in members:
                continue
            latest = await self._backend.history(group, 1)
            item = dict(
                group=group,
                name=info["display_name"] or " ↔ ".join(members),
                kind=info["kind"],
                members=list(members),
                online=sorted(online.intersection(members)),
                latest=latest[0][0] if latest else None,
            )
            if include_preview:
                preview = None
                if latest:
                    try:
                        message = Message.decode(latest[0][1]["data"])
                        preview = {"sender": message.sender, "body": message.body[:160]}
                    except (MessagingError, KeyError):
                        pass
                item["preview"] = preview
            result.append(item)
        return result

    async def targets(self) -> dict[str, Any]:
        records = await self._backend.participants()
        return {
            "participants": [
                {
                    "target": ref,
                    "name": info["display_name"],
                    "online": await self._backend.online(ref)
                    if target(ref).kind == "agent"
                    else None,
                }
                for ref, info in sorted(records.items())
                if ref != self.actor
            ],
            "groups": await self.contacts(),
        }

    async def send(
        self,
        destination: str,
        *,
        body: str,
        in_reply_to: str | None = None,
        run: str | None = None,
        thread: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        message = Message(
            message_id if message_id is not None else str(uuid4()),
            self.actor,
            body,
            in_reply_to,
            {"thread": thread, "run": run} if self._lease else None,
        )
        group = await self.resolve(destination)
        sid = await self._backend.append(
            group, self.actor, message.encode(), token=self._lease
        )
        return {"group": group, "stream_id": sid, "message": message.data()}

    async def read(
        self, group: str, *, after: str = "0-0", count: int = 100
    ) -> list[tuple[str, dict[str, str]]]:
        stream_id(after)
        if not 1 <= count <= 1000:
            raise MessagingError("Read count must be between 1 and 1000")
        await self.conversation(group)
        return await self._backend.read(group, after, count)

    async def history(
        self, group: str, *, count: int = 200
    ) -> list[tuple[str, dict[str, str]]]:
        if not 1 <= count <= 1000:
            raise MessagingError("History count must be between 1 and 1000")
        await self.conversation(group)
        return await self._backend.history(group, count)

    async def check_cursor(self, group: str, cursor: str) -> str | None:
        stream_id(cursor)
        await self.conversation(group)
        if cursor == "0-0":
            return None
        first = await self._backend.first(group)
        if not first:
            return "Conversation history is missing"
        last = await self._backend.history(group, 1)
        if stream_id(cursor) > stream_id(last[0][0]):
            raise MessagingError(
                "Stream precedes saved cursor; explicitly reset the local checkpoint"
            )
        if stream_id(cursor) < stream_id(first[0][0]):
            return "Earlier conversation history is no longer retained"
        return None
