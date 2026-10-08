"""Valkey transport and directory. Each caller owns its connection lifetime."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from valkey.asyncio import Valkey
from valkey.exceptions import ConnectionError, TimeoutError

from .config import MessagingConfig
from .errors import MessagingError, SendUnconfirmed
from .schemas import Message, component, conversation, owner_dm, stream_id

logger = logging.getLogger(__name__)
LEASE_SECONDS = 30
RENEW_SECONDS = 10
RETENTION = 10000

_REGISTER = """
local name, owner, token = ARGV[1], ARGV[2], ARGV[3]
if name == owner or redis.call('HEXISTS', KEYS[1], owner) == 1 then return 'identity collision' end
for _, human in ipairs(redis.call('HVALS', KEYS[1])) do
  if human == name then return 'identity collision' end
end
local old = redis.call('HGET', KEYS[1], name)
if old and old ~= owner then return 'owner mismatch' end
local lease = redis.call('GET', KEYS[2])
if lease and lease ~= token then return 'agent already online' end
redis.call('SET', KEYS[2], token, 'EX', ARGV[4])
redis.call('HSET', KEYS[1], name, owner)
return ''
"""
_LEASE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
if ARGV[2] == '0' then return redis.call('DEL', KEYS[1]) end
return redis.call('EXPIRE', KEYS[1], ARGV[2])
"""
_SEND = """
if ARGV[1] == 'agent' then
  if redis.call('EXISTS', KEYS[3]) == 0 then return {err='agent is offline'} end
  if redis.call('SISMEMBER', KEYS[1], ARGV[2]) == 0 then return {err='agent is not a member'} end
end
return redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[4], '*', 'data', ARGV[3])
"""


def group_key(group: str, suffix: str) -> str:
    conversation(group)
    return f"too:group:{group}:{suffix}"


def online_key(agent: str) -> str:
    return f"too:agent:{component(agent)}:online"


class MessagingClient:
    def __init__(self, config: MessagingConfig, *, client: Valkey | None = None):
        self.config = config
        self.redis = (
            client
            if client is not None
            else Valkey.from_url(
                config.url,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
            )
        )

    async def __aenter__(self) -> MessagingClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self.redis.aclose()

    async def agents(self) -> dict[str, str]:
        return await self.redis.execute_command("HGETALL", "too:agents")

    async def register(self, agent: str, owner: str, token: str) -> None:
        component(agent)
        component(owner)
        error = await self.redis.execute_command(
            "EVAL",
            _REGISTER,
            2,
            "too:agents",
            online_key(agent),
            agent,
            owner,
            token,
            LEASE_SECONDS,
        )
        if error:
            raise MessagingError(f"Could not register {agent}: {error}")
        await self.ensure_group("all")
        await self.ensure_group(owner_dm(agent))
        # Configuration owns only this agent's custom memberships.
        configured = set(self.config.groups)
        for group in await self.group_ids():
            if conversation(group).kind == "group" and group not in configured:
                await self.redis.execute_command(
                    "SREM", group_key(group, "members"), agent
                )
        for group in configured:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.sadd("too:groups", group)
                pipe.sadd(group_key(group, "members"), agent)
                await pipe.execute()

    async def renew(self, agent: str, token: str) -> None:
        ok = await self.redis.execute_command(
            "EVAL", _LEASE, 1, online_key(agent), token, LEASE_SECONDS
        )
        if not ok:
            raise MessagingError(f"Messaging lease lost for {agent}")

    async def unregister(self, agent: str, token: str) -> None:
        await self.redis.execute_command("EVAL", _LEASE, 1, online_key(agent), token, 0)

    async def group_ids(self) -> list[str]:
        result = set()
        async for group in self.redis.sscan_iter("too:groups", count=100):
            try:
                conversation(group)
            except MessagingError:
                logger.debug("Ignoring invalid conversation directory entry")
            else:
                result.add(group)
        return sorted(result)

    async def ensure_group(self, group: str) -> None:
        info = conversation(group)
        agents = await self.agents()
        if info.kind == "group":
            if not await self.redis.execute_command("SISMEMBER", "too:groups", group):
                raise MessagingError(f"Unknown group: {info.label}")
            return
        if info.kind == "public":
            members = set(agents) | set(agents.values())
            if not members:
                raise MessagingError("No messaging agents have registered")
        else:
            if any(name not in agents for name in info.names):
                raise MessagingError(f"Unknown agent in {group}")
            members = set(info.names)
            if info.kind == "owner":
                members.add(agents[info.names[0]])
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.sadd("too:groups", group)
            pipe.sadd(group_key(group, "members"), *sorted(members))
            await pipe.execute()

    async def resolve(self, target: str, *, kind: str | None = None) -> str:
        agents = await self.agents()
        if kind == "dm":
            group = owner_dm(target)
        elif kind == "group":
            group = "gc_" + component(target)
        elif target == "all" or target.startswith(("dm_", "gc_")):
            group = target
        else:
            custom = "gc_" + component(target)
            is_group = await self.redis.execute_command(
                "SISMEMBER", "too:groups", custom
            )
            if target in agents and is_group:
                raise MessagingError(
                    "Ambiguous target; use --dm or --group before the target"
                )
            if target not in agents and not is_group:
                raise MessagingError(f"Unknown messaging target: {target}")
            group = owner_dm(target) if target in agents else custom
        await self.ensure_group(group)
        return group

    async def contacts(
        self,
        *,
        agent: str | None = None,
        include_dms: bool = True,
        include_preview: bool = False,
    ) -> list[dict[str, Any]]:
        agents = await self.agents()
        if agent is not None and (
            agent not in agents or not await self.redis.exists(online_key(agent))
        ):
            raise MessagingError("Agent is not online for messaging")
        online = {
            name: bool(await self.redis.exists(online_key(name))) for name in agents
        }
        result = []
        for group in await self.group_ids():
            info = conversation(group)
            if not include_dms and info.kind in ("owner", "dm"):
                continue
            members = sorted(
                await self.redis.execute_command(
                    "SMEMBERS", group_key(group, "members")
                )
            )
            if agent is not None and agent not in members:
                continue
            latest = await self.redis.xrevrange(group_key(group, "msg"), count=1)
            result.append(
                {
                    "group": group,
                    "name": info.label,
                    "members": members,
                    "online": [name for name in members if online.get(name)],
                    "latest": latest[0][0] if latest else None,
                }
            )
            if include_preview:
                preview = None
                if latest:
                    try:
                        message = Message.decode(latest[0][1]["data"])
                    except (KeyError, MessagingError):
                        pass
                    else:
                        preview = {"sender": message.sender, "body": message.body[:160]}
                result[-1]["preview"] = preview
        return result

    async def send(
        self,
        group: str,
        *,
        sender: str,
        body: str,
        agent: bool = False,
        run: str | None = None,
        in_reply_to: str | None = None,
    ) -> dict[str, Any]:
        component(sender)
        message = Message.create(
            sender,
            body,
            in_reply_to=in_reply_to,
            origin={"agent": sender, "run": run} if agent else None,
        )
        await self.ensure_group(group)
        agents = await self.agents()
        if not agent and sender in agents:
            raise MessagingError("Human name conflicts with a registered agent")
        if agent and sender not in agents:
            raise MessagingError("Agent is not registered for messaging")
        try:
            sid = await self.redis.execute_command(
                "EVAL",
                _SEND,
                3,
                group_key(group, "members"),
                group_key(group, "msg"),
                online_key(sender),
                "agent" if agent else "human",
                sender,
                message.encode(),
                RETENTION,
            )
        except (ConnectionError, TimeoutError) as exc:
            raise SendUnconfirmed(
                f"Send not confirmed (message {message.id}); check history before retrying"
            ) from exc
        return {"group": group, "stream_id": sid, "message": message.data()}

    async def read(
        self, group: str, *, after: str = "0-0", count: int = 100
    ) -> list[tuple[str, dict[str, str]]]:
        stream_id(after)
        return await self.redis.xrange(
            group_key(group, "msg"), min=f"({after}", count=count
        )

    async def history(
        self, group: str, *, count: int = 200
    ) -> list[tuple[str, dict[str, str]]]:
        return list(
            reversed(await self.redis.xrevrange(group_key(group, "msg"), count=count))
        )

    async def check_cursor(self, group: str, cursor: str) -> str | None:
        if cursor == "0-0":
            return None
        earliest = await self.redis.xrange(group_key(group, "msg"), count=1)
        if not earliest:
            return "Conversation history is missing"
        latest = await self.redis.xrevrange(group_key(group, "msg"), count=1)
        if stream_id(cursor) > stream_id(latest[0][0]):
            raise MessagingError(
                "Stream precedes saved cursor; restore history or explicitly reset the local checkpoint"
            )
        if stream_id(cursor) < stream_id(earliest[0][0]):
            return "Earlier conversation history is no longer retained"
        return None


def host_token() -> str:
    return str(uuid4())
