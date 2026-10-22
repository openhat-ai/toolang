"""Valkey implementation of the teaming storage contract."""

from __future__ import annotations

import asyncio
import json
import random
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from valkey.asyncio import Valkey
from valkey.asyncio.retry import Retry
from valkey.backoff import NoBackoff
from valkey.exceptions import ConnectionError, TimeoutError, ValkeyError

from ...config import BackendConfig
from ...errors import (
    BackendUnavailable,
    ConversationAccessDenied,
    ConversationRulesConflict,
    LeaseLost,
    MessagingError,
    SendUnconfirmed,
    TeamingError,
    StorageIntegrityError,
)
from .keys import Keys
from ...ids import dm_id, gc_id
from ...schemas import (
    HUB_API_VERSION,
    Conversation,
    ConversationRules,
    TeamMember,
    ConversationRecord,
    RosterRecord,
    TeamEvent,
    TeamRecord,
    conversation_id,
    conversation_name,
    participant,
    target,
    TalkCursor,
    rules_revision,
)
from . import scripts
from .lua import ROOT_GUARD, TEAM_GUARD
from .events import ValkeyEvents
from ...types import SNAPSHOT_RETRIES, STORAGE_BATCH_SIZE


class _SnapshotChanged(Exception):
    """The atomic operation rejected a stale, previously validated read."""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def _snapshot_backoff(attempt: int) -> None:
    """Desynchronize contenders after a rejected, definitely unwritten operation."""
    if attempt + 1 < SNAPSHOT_RETRIES:
        ceiling = min(0.1, 0.005 * 2**attempt)
        await asyncio.sleep(random.uniform(ceiling / 2, ceiling))


class ValkeyBackend:
    def __init__(self, config: BackendConfig, *, client: Valkey | None = None):
        self.keys = Keys(config.root_code)
        self._root = config.root
        self._initialized = False
        self.events = ValkeyEvents(self)
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
        caller = asyncio.current_task()
        cancelling = caller.cancelling() if caller is not None else 0
        try:
            if command.upper() not in {"PING", "EVAL"}:
                await self._client.execute_command(
                    "EVAL", ROOT_GUARD, 1, self.keys.root, self._root
                )
            result = await self._client.execute_command(command, *args)
        except (ConnectionError, TimeoutError) as exc:
            raise BackendUnavailable("Teaming backend is unavailable") from exc
        except ValkeyError as exc:
            if "snapshot_changed" in str(exc):
                raise _SnapshotChanged from exc
            if "integrity:" in str(exc):
                raise StorageIntegrityError(str(exc)) from exc
            if "Agent lease lost" in str(exc):
                raise LeaseLost(str(exc)) from exc
            if "conversation_rules_conflict" in str(exc):
                raise ConversationRulesConflict(
                    "Conversation rules changed; read again before retrying"
                ) from exc
            if "conversation_access_denied:" in str(exc):
                raise ConversationAccessDenied(
                    "Agent is not a member of this conversation"
                ) from exc
            raise MessagingError(str(exc)) from exc
        # Python 3.11 wait_for can lose cancellation when a socket write completes.
        if caller is not None and caller.cancelling() > cancelling:
            raise asyncio.CancelledError
        return result

    async def _eval(self, script: str, keys: list[str], args: list[object]) -> Any:
        if script != scripts.INITIALIZE:
            script = ROOT_GUARD + script
        return await self._call(
            "EVAL", script, len(keys) + 1, *keys, self.keys.root, *args, self._root
        )

    async def _read(self, command: str, key: str, *args: object) -> Any:
        """Read data in the same atomic operation as its root ownership check."""
        return await self._eval(
            "return redis.call(ARGV[1],KEYS[1],unpack(ARGV,2))",
            [key],
            [command, *args],
        )

    async def _fenced_eval(
        self, agent: str, script: str, keys: list[str], args: list[object]
    ) -> Any:
        """Validate a publisher record and fence the exact bytes before mutation.

        The script still checks the current lease and deadline atomically. Retry
        only a rejected snapshot, never an uncertain transport result.
        """
        target(agent, kind="agent")
        for attempt in range(SNAPSHOT_RETRIES):
            raw = await self._read("HGET", self.keys.team_members, agent)
            try:
                if raw is not None:
                    TeamRecord.decode(agent, raw)
            except (ValueError, TypeError, MessagingError) as exc:
                raise StorageIntegrityError("Invalid team record") from exc
            checked = _json(
                {
                    "key": keys.index(self.keys.team_members) + 1,
                    "member": agent,
                    "raw": raw,
                }
            )
            try:
                return await self._eval(TEAM_GUARD + script, keys, [*args, checked])
            except _SnapshotChanged:
                await _snapshot_backoff(attempt)
        raise MessagingError("Teaming state changed repeatedly; retry the operation")

    async def initialize(self) -> None:
        epoch = uuid4().hex
        await self._eval(
            scripts.INITIALIZE,
            self.keys.base,
            [
                _json(
                    {
                        "epoch": epoch,
                        "initial_event": TeamEvent(
                            v=HUB_API_VERSION,
                            epoch=epoch,
                            type="stream.initialized",
                            conversation=None,
                            actor=None,
                            payload={},
                        ).model_dump_json(),
                    }
                ),
                self.keys.prefix,
                int(self._initialized),
            ],
        )
        self._initialized = True
        await self._operation(scripts.READ, {})

    async def _snapshot(self, op: dict) -> dict:
        request = {
            key: op[key]
            for key in (
                "actor",
                "agent",
                "owner",
                "id",
                "participants",
                "conversations",
            )
            if key in op
        }
        request.update(
            prefix=self.keys.prefix,
            rules=op.get("inspect_rules", False),
            action=op.get("action")
            if op.get("action") in {"team", "roster", "contacts", "register"}
            else "inspect",
        )
        snapshot = json.loads(
            await self._eval(scripts.SNAPSHOT, self.keys.base, [_json(request)])
        )
        snapshot["members"] = dict(snapshot["members"])
        try:
            records = {}
            for kind, key, field, raw in snapshot["records"]:
                if raw is None:
                    if kind == "event":
                        raise ValueError("Missing team event data")
                    continue
                if kind == "system":
                    conversation_id(raw)
                    continue
                model = (
                    TeamRecord.decode(field, raw)
                    if kind == "team"
                    else {
                        "conversation": ConversationRecord,
                        "roster": RosterRecord,
                        "event": TeamEvent,
                        "rules": ConversationRules,
                    }[kind].model_validate_json(raw)
                )
                records[kind, field] = model
                if isinstance(
                    model, ConversationRules
                ) and model.conversation != op.get("id"):
                    raise ValueError("Rules conversation differs from its storage key")
                if isinstance(model, ConversationRecord):
                    if model.id != field:
                        raise ValueError("Conversation ID differs from its hash field")
                    if model.created_by is None and field != snapshot["system"]:
                        raise ValueError("Only the system conversation has no creator")
                elif isinstance(model, RosterRecord):
                    target(field, kind="agent")
            for key, members in snapshot["members"].items():
                for member in members:
                    participant(member)
                ref = key.removeprefix(self.keys.prefix + ":convo:").removesuffix(
                    ":members"
                )
                record = records.get(("conversation", ref))
                if (
                    isinstance(record, ConversationRecord)
                    and record.kind == "dm"
                    and len(members) != 2
                ):
                    raise StorageIntegrityError("Invalid DM membership")
            for (kind, member), _record in records.items():
                if kind == "roster" and ("team", member) not in records:
                    raise ValueError("Roster entry has no team member")
        except StorageIntegrityError:
            raise
        except (ValueError, TypeError, KeyError, MessagingError) as exc:
            raise StorageIntegrityError("Invalid teaming protocol record") from exc
        return snapshot

    async def _operation(
        self, script: str, op: dict, extra: list[str] | None = None
    ) -> Any:
        for attempt in range(SNAPSHOT_RETRIES):
            snapshot = await self._snapshot(op)
            seconds, micros = map(int, snapshot["clock"])
            stamp = datetime.fromtimestamp(seconds, UTC).replace(microsecond=micros)
            checked = {
                **op,
                "checked": snapshot["records"],
                "checked_members": snapshot["members"],
                "checked_hashes": snapshot["hashes"],
                "timestamp": stamp.isoformat(timespec="milliseconds").replace(
                    "+00:00", "Z"
                ),
            }
            try:
                return await self._eval(
                    script, [*self.keys.base, *(extra or [])], [_json(checked)]
                )
            except _SnapshotChanged:
                await _snapshot_backoff(attempt)
        raise MessagingError("Teaming state changed repeatedly; retry the operation")

    async def team(self) -> list[TeamMember]:
        return list(json.loads(await self._operation(scripts.READ, {"action": "team"})))

    async def roster(self) -> dict[str, dict]:
        rows = await self._operation(scripts.READ, {"action": "roster"})
        return {
            member: json.loads(raw)
            for member, raw in zip(rows[::2], rows[1::2], strict=True)
        }

    async def reconcile_roster(
        self, agent: str, owner: str, previous: dict | None, updated: dict | None
    ) -> bool:
        target(agent, kind="agent")
        target(owner, kind="human")
        if previous is not None:
            RosterRecord.model_validate(previous)
        if updated is not None:
            RosterRecord.model_validate(updated)
        # Retain the stored bytes for CAS; JSON object ordering has no semantics.
        raw = await self._read("HGET", self.keys.roster, agent)
        try:
            current = (
                RosterRecord.model_validate_json(raw).model_dump()
                if raw is not None
                else None
            )
        except (ValueError, TypeError) as exc:
            raise StorageIntegrityError("Invalid roster record") from exc
        if current != previous:
            return False
        conversations = await self.conversation_ids() if updated is None else []
        return bool(
            await self._operation(
                scripts.ROSTER,
                dict(
                    agent=agent,
                    owner=owner,
                    previous=raw or "",
                    updated=_json(updated) if updated is not None else "",
                    conversations=conversations,
                ),
                [self.keys.convo_key(ref, "members") for ref in conversations],
            )
        )

    async def known_participant(self, member: str) -> bool:
        participant(member)
        return bool(await self._read("HEXISTS", self.keys.team_members, member))

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
            try:
                data = TeamEvent.model_validate_json(fields["data"]).model_dump()
            except (ValueError, TypeError, KeyError, MessagingError) as exc:
                raise StorageIntegrityError("Invalid team event record") from exc
            rows.append((sid, data))
        return rows

    async def wait_team_events(self, after: str) -> None:
        # Blocking reads are wake-up hints; replay validates each batch atomically.
        await self._call(
            "XREAD", "COUNT", 1, "BLOCK", 1000, "STREAMS", self.keys.team_events, after
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
        existing = await self._read("HGET", self.keys.system, "all")
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
            [self.keys.convo_key(system, "members")],
        )

    async def renew_lease(self, agent: str, token: str) -> bool:
        return bool(
            await self._operation(
                scripts.PRESENCE,
                dict(action="renew", agent=agent, token=token),
            )
        )

    async def release_lease(self, agent: str, token: str) -> bool:
        return bool(
            await self._operation(
                scripts.PRESENCE,
                dict(action="release", agent=agent, token=token),
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

    def _conversation_keys(
        self, conversation: str, name: str | None = None
    ) -> list[str]:
        return [
            self.keys.convo_key(conversation, "members"),
            self.keys.convo_key(conversation, "messages"),
            self.keys.name_key(name),
            self.keys.convo_key(conversation, "rules"),
        ]

    async def conversation_ids(self) -> list[str]:
        return await self._operation(scripts.READ, {"action": "ids"})

    async def contacts(
        self, actor: str
    ) -> list[tuple[Conversation, list[tuple[str, dict[str, str]]]]]:
        ids = await self.conversation_ids()
        result = []
        for start in range(0, len(ids), STORAGE_BATCH_SIZE):
            batch = ids[start : start + STORAGE_BATCH_SIZE]
            rows = json.loads(
                await self._operation(
                    scripts.READ,
                    {"action": "contacts", "actor": actor, "conversations": batch},
                    [
                        self.keys.convo_key(ref, suffix)
                        for ref in batch
                        for suffix in ("members", "messages")
                    ],
                )
            )
            for info, members, latest in rows:
                result.append(
                    (
                        Conversation(**info, participants=tuple(sorted(members))),
                        self._messages(latest),
                    )
                )
        return result

    async def named(self, name: str) -> list[str]:
        conversation_name(name)
        await self._operation(scripts.READ, {})
        return sorted(await self._read("SMEMBERS", self.keys.name_key(name)))

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
            [
                self.keys.convo_key(conversation, "members"),
                self.keys.name_key(None),
                self.keys.name_key(None),
            ],
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
                self.keys.convo_key(conversation, "members"),
                self.keys.name_key(previous.name),
                self.keys.name_key(name),
            ],
        )

    async def get_rules(self, conversation: str, actor: str) -> ConversationRules:
        conversation_id(conversation)
        raw = await self._operation(
            scripts.RULES,
            dict(action="read", id=conversation, actor=actor, inspect_rules=True),
            self._conversation_keys(conversation),
        )
        return (
            ConversationRules.model_validate_json(raw)
            if raw
            else ConversationRules.create(conversation)
        )

    async def set_rules(
        self, conversation: str, actor: str, *, content: str, revision: str, token: str
    ) -> ConversationRules:
        document = ConversationRules.create(conversation, content)
        rules_revision(revision)
        await self._operation(
            scripts.RULES,
            dict(
                action="write",
                id=conversation,
                actor=actor,
                token=token,
                revision=revision,
                document=document.model_dump_json(),
                inspect_rules=True,
            ),
            self._conversation_keys(conversation),
        )
        return document

    async def append(
        self,
        conversation: str,
        actor: str,
        data: str,
        *,
        token: str,
        pair: tuple[str, ...] = (),
        rules_revision: str | None = None,
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
                    rules_revision=rules_revision,
                    inspect_rules=rules_revision is not None,
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
        return self._messages(rows)

    @staticmethod
    def _messages(rows: list) -> list[tuple[str, dict[str, str]]]:
        return [
            (sid, dict(zip(fields[::2], fields[1::2], strict=True)))
            for sid, fields in rows
        ]

    async def talk_cursor(self, agent: str, convo_id: str) -> str:
        target(agent, kind="agent")
        conversation_id(convo_id)
        value = await self._operation(
            scripts.TALK_CURSOR,
            dict(action="read", actor=agent, id=convo_id),
            [
                self.keys.convo_key(convo_id, "members"),
                self.keys.convo_key(convo_id, "messages"),
                self.keys.talk_key(agent),
            ],
        )
        try:
            return TalkCursor(cursor=value).cursor
        except (ValueError, MessagingError) as exc:
            raise StorageIntegrityError("Invalid talk cursor") from exc

    async def advance_talk_cursor(
        self, agent: str, convo_id: str, cursor: str, *, token: str
    ) -> None:
        target(agent, kind="agent")
        conversation_id(convo_id)
        cursor = TalkCursor(cursor=cursor).cursor
        await self._operation(
            scripts.TALK_CURSOR,
            dict(
                action="advance", actor=agent, id=convo_id, cursor=cursor, token=token
            ),
            [
                self.keys.convo_key(convo_id, "members"),
                self.keys.convo_key(convo_id, "messages"),
                self.keys.talk_key(agent),
            ],
        )

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
