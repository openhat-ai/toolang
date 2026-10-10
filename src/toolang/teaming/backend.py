"""The only Redis/Valkey driver boundary for teaming."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from valkey.asyncio import Valkey
from valkey.asyncio.retry import Retry
from valkey.backoff import NoBackoff
from valkey.exceptions import ConnectionError, TimeoutError, ValkeyError

from .config import BackendConfig
from .errors import (
    BackendUnavailable,
    ConversationAccessDenied,
    LeaseLost,
    MessagingError,
    SendUnconfirmed,
    TeamingError,
    StorageIntegrityError,
)
from .keys import (
    BASE_KEYS,
    PREFIX,
    CONVOS,
    SYSTEM,
    TEAM,
    TEAM_EVENTS,
    convo_key,
    name_key,
)
from .ids import dm_id, gc_id
from .schemas import (
    Conversation,
    conversation_id,
    conversation_name,
    participant,
    target,
)
from . import storage_scripts as scripts

LEASE_SECONDS = 15
RETENTION = 10000


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Backend:
    def __init__(self, config: BackendConfig, *, client: Valkey | None = None):
        self._initialized = False
        try:
            self._client = (
                client
                if client is not None
                else Valkey.from_url(
                    config.url,
                    decode_responses=True,
                    socket_timeout=5,
                    socket_connect_timeout=5,
                )
            )
            # URL retry options must never replay an uncertain append.
            self._client.set_retry(Retry(NoBackoff(), 0))
        except (ValueError, ValkeyError) as exc:
            raise TeamingError("Invalid teaming.backend.url") from exc

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except ValkeyError as exc:
            raise BackendUnavailable("Could not close the teaming backend") from exc

    async def ping(self) -> None:
        await self._call("PING")

    async def _call(self, command: str, *args: Any) -> Any:
        try:
            return await self._client.execute_command(command, *args)
        except (ConnectionError, TimeoutError) as exc:
            raise BackendUnavailable("Teaming backend is unavailable") from exc
        except ValkeyError as exc:
            if "integrity:" in str(exc):
                raise StorageIntegrityError(str(exc)) from exc
            if "Agent lease lost" in str(exc):
                raise LeaseLost(str(exc)) from exc
            if "conversation_access_denied:" in str(exc):
                raise ConversationAccessDenied(
                    "Agent is not a member of this conversation"
                ) from exc
            raise MessagingError(str(exc)) from exc

    async def _eval(self, script: str, keys: list[str], args: list[object]) -> Any:
        return await self._call("EVAL", script, len(keys), *keys, *args)

    async def initialize(self) -> None:
        await self._eval(
            scripts.INITIALIZE, BASE_KEYS, [uuid4().hex, PREFIX, int(self._initialized)]
        )
        self._initialized = True

    async def _operation(
        self, script: str, op: dict, extra: list[str] | None = None
    ) -> Any:
        return await self._eval(script, [*BASE_KEYS, *(extra or [])], [_json(op)])

    async def team(self) -> list[dict[str, Any]]:
        return list(json.loads(await self._operation(scripts.READ, {"action": "team"})))

    async def known_participant(self, member: str) -> bool:
        participant(member)
        return bool(await self._call("HEXISTS", TEAM, member))

    async def team_event_position(self) -> tuple[str, str]:
        epoch, tail = await self._operation(scripts.READ, {"action": "metadata"})
        return epoch, tail

    async def team_event_replay(
        self, epoch: str, after: str, *, actor: str | None = None, token: str = ""
    ) -> list[tuple[str, dict]] | None:
        operation = dict(epoch=epoch, after=after)
        if actor is not None:
            operation.update(actor=actor, token=token)
        result = await self._operation(scripts.REPLAY, operation)
        if result[0] == "resync":
            return None
        rows = []
        for sid, values in result[1]:
            fields = dict(zip(values[::2], values[1::2], strict=True))
            rows.append((sid, json.loads(fields["data"])))
        return rows

    async def wait_team_events(self, after: str) -> None:
        # Blocking reads are wake-up hints; replay validates each batch atomically.
        await self._call(
            "XREAD", "COUNT", 1, "BLOCK", 1000, "STREAMS", TEAM_EVENTS, after
        )

    async def participants(self) -> dict[str, dict[str, Any]]:
        return {
            row["member"]: {k: v for k, v in row.items() if k != "member"}
            for row in await self.team()
        }

    async def lease_info(self, agent: str) -> dict[str, Any]:
        target(agent, kind="agent")
        return json.loads(
            await self._operation(
                scripts.PRESENCE, {"action": "inspect", "agent": agent}
            )
        )

    async def online(self, agent: str) -> bool:
        return (await self.lease_info(agent))["lease"] is not None

    async def due_presence(self) -> list[str]:
        return await self._operation(scripts.READ, {"action": "due"})

    async def expire_presence(self, agent: str) -> bool:
        return bool(
            await self._operation(
                scripts.PRESENCE, {"action": "expire", "agent": agent}
            )
        )

    async def system_conversation(self) -> str:
        existing = await self._call("HGET", SYSTEM, "all")
        if existing:
            return conversation_id(existing)
        return await self.create_gc(None, name="all", system=True)

    async def register(
        self,
        human: str,
        *,
        agent: str | None = None,
        token: str = "",
        endpoint: str = "",
        root: str = "",
    ) -> None:
        target(human, kind="human")
        if agent is not None:
            target(agent, kind="agent")
            if not token:
                raise MessagingError("Agent lease token must be nonempty text")
        system = await self.system_conversation()
        await self._operation(
            scripts.PRESENCE,
            dict(
                action="register",
                owner=human,
                agent=agent or human,
                token=token,
                endpoint=endpoint,
                root=root,
                system=system,
            ),
            [convo_key(system, "members")],
        )

    async def lease(self, agent: str, token: str, seconds: int) -> bool:
        return bool(
            await self._operation(
                scripts.PRESENCE,
                dict(
                    action="renew" if seconds else "release", agent=agent, token=token
                ),
            )
        )

    async def conversation(
        self, conversation: str, actor: str, *, pair: tuple[str, ...] = ()
    ) -> Conversation | None:
        conversation_id(conversation)
        result = await self._operation(
            scripts.CONVERSATION,
            dict(action="lookup", id=conversation, actor=actor, participants=pair),
            self._conversation_keys(conversation),
        )
        if not result:
            return None
        info = json.loads(result[0])
        conversation_name(info["name"])
        if info["created_by"] is not None:
            participant(info["created_by"])
        members = tuple(sorted(result[1]))
        for member in members:
            participant(member)
        return Conversation(**info, participants=members)

    @staticmethod
    def _conversation_keys(conversation: str, name: str | None = None) -> list[str]:
        return [
            convo_key(conversation, "members"),
            convo_key(conversation, "messages"),
            name_key(name),
        ]

    async def conversation_ids(self) -> list[str]:
        await self._operation(scripts.READ, {})
        return await self._call("HKEYS", CONVOS)

    async def named(self, name: str) -> list[str]:
        conversation_name(name)
        await self._operation(scripts.READ, {})
        return sorted(await self._call("SMEMBERS", name_key(name)))

    async def create_dm(
        self,
        actor: str,
        pair: tuple[str, str],
        *,
        token: str = "",
        name: str | None = None,
    ) -> str:
        conversation = dm_id(*pair)
        conversation_name(name)
        return await self._operation(
            scripts.CONVERSATION,
            dict(
                action="create",
                id=conversation,
                kind="dm",
                actor=actor,
                token=token,
                participants=pair,
                name=name,
            ),
            self._conversation_keys(conversation, name),
        )

    async def create_gc(
        self,
        actor: str | None,
        *,
        token: str = "",
        name: str | None = None,
        system: bool = False,
    ) -> str:
        conversation_name(name)
        for _ in range(128):
            tick, seq = await self._operation(
                scripts.RESERVE, {} if system else dict(actor=actor, token=token)
            )
            conversation = gc_id(tick, seq)
            result = await self._operation(
                scripts.CONVERSATION,
                dict(
                    action="create",
                    id=conversation,
                    kind="gc",
                    actor=actor,
                    token=token,
                    name=name,
                    system=system,
                ),
                self._conversation_keys(conversation, name),
            )
            if result:
                return result
        raise MessagingError("Could not allocate a conversation after 128 conflicts")

    async def member(
        self, conversation: str, actor: str, *, token: str, join: bool
    ) -> None:
        await self._operation(
            scripts.EDIT,
            dict(action="member", id=conversation, actor=actor, token=token, join=join),
            [convo_key(conversation, "members"), name_key(None), name_key(None)],
        )

    async def rename(
        self,
        conversation: str,
        actor: str,
        *,
        token: str,
        name: str | None,
        revision: int,
    ) -> None:
        conversation_name(name)
        previous = await self.conversation(conversation, actor)
        if previous is None:
            raise MessagingError("Unknown conversation")
        await self._operation(
            scripts.EDIT,
            dict(
                action="rename",
                id=conversation,
                actor=actor,
                token=token,
                name=name,
                previous=previous.name,
                revision=revision,
            ),
            [
                convo_key(conversation, "members"),
                name_key(previous.name),
                name_key(name),
            ],
        )

    async def append(
        self,
        conversation: str,
        actor: str,
        data: str,
        *,
        token: str,
        pair: tuple[str, ...] = (),
    ) -> str:
        if pair and (len(pair) != 2 or dm_id(pair[0], pair[1]) != conversation):
            raise MessagingError("DM pair does not match conversation ID")
        try:
            return await self._operation(
                scripts.CONVERSATION,
                dict(
                    action="send",
                    id=conversation,
                    kind=conversation[:2],
                    actor=actor,
                    token=token,
                    data=data,
                    participants=pair,
                ),
                self._conversation_keys(conversation),
            )
        except BackendUnavailable as exc:
            raise SendUnconfirmed(
                f"Send not confirmed (message {json.loads(data)['id']}); check history before resending"
            ) from exc

    async def messages(
        self,
        conversation: str,
        actor: str,
        *,
        start: str,
        finish: str,
        count: int,
        reverse: bool = False,
    ) -> list[tuple[str, dict[str, str]]]:
        rows = await self._operation(
            scripts.READ,
            dict(
                action="messages",
                id=conversation,
                actor=actor,
                start=start,
                finish=finish,
                count=count,
                reverse=reverse,
            ),
            self._conversation_keys(conversation),
        )
        return [
            (sid, dict(zip(fields[::2], fields[1::2], strict=True)))
            for sid, fields in rows
        ]

    async def statistics(
        self, actor: str, conversation: str | None = None
    ) -> dict[str, int | str | None]:
        return json.loads(
            await self._operation(
                scripts.READ,
                dict(action="stats", actor=actor, id=conversation),
                self._conversation_keys(conversation) if conversation else None,
            )
        )
