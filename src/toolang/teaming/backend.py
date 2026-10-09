"""The only Redis/Valkey driver boundary for teaming."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from valkey.asyncio import Valkey
from valkey.asyncio.retry import Retry
from valkey.backoff import NoBackoff
from valkey.exceptions import ConnectionError, TimeoutError, ValkeyError

from .config import BackendConfig
from .errors import (
    BackendUnavailable,
    LeaseLost,
    MessagingError,
    SendUnconfirmed,
    TeamingError,
)
from .schemas import direct_pair, target

PREFIX = "too:teaming:v1"
PARTICIPANTS = f"{PREFIX}:participants"
GROUPS = f"{PREFIX}:msg:groups"
DIRECT = f"{PREFIX}:msg:direct"
LEASE_SECONDS = 30
RETENTION = 10000


def group_key(group: str, suffix: str) -> str:
    return f"{PREFIX}:msg:group:{target(group, kind='group').name}:{suffix}"


def online_key(agent: str) -> str:
    return f"{PREFIX}:agent:{target(agent, kind='agent').name}:online"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _group_record(
    kind: str, creator: str | None, name: str | None = None, system: bool = False
) -> str:
    return _json(
        dict(
            kind=kind,
            display_name=name,
            created_by=creator,
            created_at=_now(),
            system=system,
        )
    )


_TYPES = """
local function expect(key, expected)
  local actual = redis.call('TYPE', key).ok
  if actual ~= 'none' and actual ~= expected then error('Invalid teaming key type') end
end
"""
_REGISTER = (
    _TYPES
    + """
expect(KEYS[1], 'hash'); expect(KEYS[2], 'hash'); expect(KEYS[3], 'set')
if ARGV[4] ~= '' then
  expect(KEYS[4], 'hash')
  local old = redis.call('HGET', KEYS[1], ARGV[4])
  if old and cjson.decode(old).owner ~= ARGV[1] then return {err='Agent owner mismatch'} end
  local token = redis.call('HGET', KEYS[4], 'token')
  if token and token ~= ARGV[6] then return {err='Agent already online'} end
end
redis.call('HSETNX', KEYS[1], ARGV[1], ARGV[2])
redis.call('HSETNX', KEYS[2], 'group:all', ARGV[3])
redis.call('SADD', KEYS[3], ARGV[1])
if ARGV[4] ~= '' then
  redis.call('HSETNX', KEYS[1], ARGV[4], ARGV[5])
  redis.call('SADD', KEYS[3], ARGV[4])
  redis.call('HSET', KEYS[4], 'token', ARGV[6], 'endpoint', ARGV[7])
  redis.call('EXPIRE', KEYS[4], ARGV[8])
end
return 1
"""
)
_LEASE = """
if redis.call('HGET', KEYS[1], 'token') ~= ARGV[1] then return 0 end
if ARGV[2] == '0' then return redis.call('DEL', KEYS[1]) end
return redis.call('EXPIRE', KEYS[1], ARGV[2])
"""
_CREATE = (
    _TYPES
    + """
expect(KEYS[1], 'hash'); expect(KEYS[2], 'set'); expect(KEYS[3], 'hash'); expect(KEYS[4], 'hash')
if ARGV[4] ~= '' then
  local existing = redis.call('HGET', KEYS[3], ARGV[4])
  if existing then return existing end
end
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 or redis.call('EXISTS', KEYS[2], KEYS[6]) > 0 then return '' end
for i = 6, #ARGV do
  if redis.call('HEXISTS', KEYS[4], ARGV[i]) == 0 then return {err='Unknown participant'} end
end
if ARGV[5] ~= '' and redis.call('HGET', KEYS[5], 'token') ~= ARGV[5] then return {err='Agent lease lost'} end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
for i = 6, #ARGV do redis.call('SADD', KEYS[2], ARGV[i]) end
if ARGV[4] ~= '' then redis.call('HSET', KEYS[3], ARGV[4], ARGV[1]) end
return ARGV[1]
"""
)
_MEMBER = (
    _TYPES
    + """
expect(KEYS[1], 'hash'); expect(KEYS[2], 'set'); expect(KEYS[3], 'hash')
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return {err='Unknown group'} end
local info = cjson.decode(raw)
if info.kind ~= 'group' or info.system then return {err='Cannot edit direct/system membership'} end
if redis.call('HEXISTS', KEYS[3], ARGV[2]) == 0 then return {err='Unknown participant'} end
if ARGV[3] ~= '' and redis.call('HGET', KEYS[4], 'token') ~= ARGV[3] then return {err='Agent lease lost'} end
return redis.call(ARGV[4], KEYS[2], ARGV[2])
"""
)
_SEND = (
    _TYPES
    + """
expect(KEYS[1], 'hash'); expect(KEYS[2], 'set'); expect(KEYS[3], 'stream')
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 0 then return {err='Unknown group'} end
if redis.call('SISMEMBER', KEYS[2], ARGV[2]) == 0 then return {err='Conversation is read-only for nonparticipants'} end
if ARGV[3] ~= '' and redis.call('HGET', KEYS[4], 'token') ~= ARGV[3] then return {err='Agent lease lost'} end
return redis.call('XADD', KEYS[3], 'MAXLEN', '~', ARGV[5], '*', 'data', ARGV[4])
"""
)


class Backend:
    def __init__(self, config: BackendConfig, *, client: Valkey | None = None):
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
            if str(exc) == "Agent lease lost":
                raise LeaseLost(str(exc)) from exc
            raise MessagingError(str(exc)) from exc

    async def _eval(self, script: str, keys: list[str], args: list[object]) -> Any:
        return await self._call("EVAL", script, len(keys), *keys, *args)

    async def participants(self) -> dict[str, dict[str, Any]]:
        return {
            k: json.loads(v)
            for k, v in (await self._call("HGETALL", PARTICIPANTS)).items()
        }

    async def groups(self) -> dict[str, dict[str, Any]]:
        return {
            k: json.loads(v) for k, v in (await self._call("HGETALL", GROUPS)).items()
        }

    async def members(self, group: str) -> tuple[str, ...]:
        return tuple(sorted(await self._call("SMEMBERS", group_key(group, "members"))))

    async def online(self, agent: str) -> bool:
        return bool(await self._call("EXISTS", online_key(agent)))

    async def register(
        self,
        human: str,
        *,
        agent: str | None = None,
        token: str = "",
        endpoint: str = "",
    ) -> None:
        human_name = target(human, kind="human").name
        agent_record = ""
        if agent is not None:
            agent_name = target(agent, kind="agent").name
            agent_record = _json(
                dict(display_name=agent_name, owner=human, created_at=_now())
            )
        await self._eval(
            _REGISTER,
            [
                PARTICIPANTS,
                GROUPS,
                group_key("group:all", "members"),
                online_key(agent) if agent else PARTICIPANTS,
            ],
            [
                human,
                _json(dict(display_name=human_name, owner=None, created_at=_now())),
                _group_record("group", None, "all", True),
                agent or "",
                agent_record,
                token,
                endpoint,
                LEASE_SECONDS,
            ],
        )

    async def lease(self, agent: str, token: str, seconds: int) -> bool:
        return bool(await self._eval(_LEASE, [online_key(agent)], [token, seconds]))

    async def create(
        self, group: str, actor: str, *, token: str = "", other: str | None = None
    ) -> bool:
        pair = direct_pair(actor, other) if other is not None else ""
        result = await self._eval(
            _CREATE,
            [
                GROUPS,
                group_key(group, "members"),
                DIRECT,
                PARTICIPANTS,
                online_key(actor) if token else PARTICIPANTS,
                group_key(group, "messages"),
            ],
            [
                group,
                _group_record(
                    "direct" if other else "group",
                    actor,
                    None if other else target(group).name,
                ),
                actor,
                pair,
                token,
                actor,
                *([other] if other else []),
            ],
        )
        return result == group

    async def direct(self, a: str, b: str) -> str | None:
        return await self._call("HGET", DIRECT, direct_pair(a, b))

    async def member(self, group: str, actor: str, *, token: str, join: bool) -> None:
        await self._eval(
            _MEMBER,
            [
                GROUPS,
                group_key(group, "members"),
                PARTICIPANTS,
                online_key(actor) if token else PARTICIPANTS,
            ],
            [group, actor, token, "SADD" if join else "SREM"],
        )

    async def append(self, group: str, actor: str, data: str, *, token: str) -> str:
        try:
            return await self._eval(
                _SEND,
                [
                    GROUPS,
                    group_key(group, "members"),
                    group_key(group, "messages"),
                    online_key(actor) if token else PARTICIPANTS,
                ],
                [group, actor, token, data, RETENTION],
            )
        except BackendUnavailable as exc:
            raise SendUnconfirmed(
                f"Send not confirmed (message {json.loads(data)['id']}); check history before resending"
            ) from exc

    async def read(
        self, group: str, after: str, count: int
    ) -> list[tuple[str, dict[str, str]]]:
        return await self._call(
            "XRANGE", group_key(group, "messages"), f"({after}", "+", "COUNT", count
        )

    async def history(self, group: str, count: int) -> list[tuple[str, dict[str, str]]]:
        return list(
            reversed(
                await self._call(
                    "XREVRANGE", group_key(group, "messages"), "+", "-", "COUNT", count
                )
            )
        )

    async def first(self, group: str) -> list[tuple[str, dict[str, str]]]:
        return await self._call(
            "XRANGE", group_key(group, "messages"), "-", "+", "COUNT", 1
        )
