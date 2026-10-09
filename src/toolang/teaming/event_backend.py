"""Atomic, fenced event storage through the package-owned backend boundary."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any
from uuid import uuid4

from .backend import Backend, PREFIX, PARTICIPANTS, online_key
from .errors import MessagingError, EventProtocolError, EventRecoveryRequired
from .events import (
    HubCursor,
    MAX_STREAM_BYTES,
    MAX_STREAM_EVENTS,
    encode,
)
from .records import MAX_BYTES, MAX_ENTITIES
from .schemas import stream_id, target

EVENTS = f"{PREFIX}:events"
META, STREAM, AGENTS = f"{EVENTS}:meta", f"{EVENTS}:stream", f"{EVENTS}:agents"
MANIFEST = '["manifest"]'


def generation_key(agent: str, generation: str) -> str:
    return f"{EVENTS}:agent:{target(agent, kind='agent').name}:{generation}"


# Stream ID components are decimal strings, including values beyond Lua's exact integers.
_COMMON = """
local function expect(key, expected)
  local actual = redis.call('TYPE', key).ok
  if actual ~= 'none' and actual ~= expected then error('protocol_error: key type') end
end
local function less(a,b)
  local am,as = string.match(a,'^(%d+)%-(%d+)$')
  local bm,bs = string.match(b,'^(%d+)%-(%d+)$')
  if not am or not bm then error('protocol_error: position') end
  if #am ~= #bm then return #am < #bm end
  if am ~= bm then return am < bm end
  if #as ~= #bs then return #as < #bs end
  return as < bs
end
expect(KEYS[1],'hash'); expect(KEYS[2],'stream'); expect(KEYS[3],'hash')
local epoch = redis.call('HGET',KEYS[1],'epoch')
local function valid()
  if not epoch or redis.call('HGET',KEYS[1],'v') ~= '1' or redis.call('HEXISTS',KEYS[1],'pending') == 1 then error('protocol_error: incomplete dataset') end
  local tail = redis.call('HGET',KEYS[1],'tail')
  local floor = redis.call('HGET',KEYS[1],'floor')
  if not tail or not floor or not redis.call('HGET',KEYS[1],'bytes') then error('protocol_error: missing metadata') end
  if tail ~= '0-0' and redis.call('EXISTS',KEYS[2]) == 0 then error('protocol_error: missing stream') end
  if #epoch ~= 32 or not string.match(epoch,'^[0-9a-f]+$') then error('protocol_error: epoch') end
  local bytes=tonumber(redis.call('HGET',KEYS[1],'bytes'))
  local revision=redis.call('HGET',KEYS[1],'catalog_revision')
  if not bytes or bytes < 0 or not revision or not string.match(revision,'^%d+$') then error('protocol_error: accounting') end
  less(tail,tail); less(floor,floor)

end
local function append(kind,agent,data)
  local sid = redis.call('XADD',KEYS[2],'*','kind',kind,'agent',agent,'data',data)
  local bytes = tonumber(redis.call('HGET',KEYS[1],'bytes')) + #kind + #agent + #data + 128
  redis.call('HSET',KEYS[1],'tail',sid)
  while redis.call('XLEN',KEYS[2]) > tonumber(ARGV[2]) or bytes > tonumber(ARGV[3]) do
    local row = redis.call('XRANGE',KEYS[2],'-','+','COUNT',1)[1]
    if not row then error('protocol_error: accounting') end
    bytes = bytes - 128
    local values = row[2]
    for i=2,#values,2 do bytes = bytes - #values[i] end
    redis.call('XDEL',KEYS[2],row[1])
    redis.call('HSET',KEYS[1],'floor',row[1])
  end
  redis.call('HSET',KEYS[1],'bytes',bytes)
  return sid
end
"""
_INIT = (
    _COMMON
    + """
if not epoch then
  if redis.call('EXISTS',KEYS[1],KEYS[2],KEYS[3]) ~= 0 then error('protocol_error: partial dataset') end
  redis.call('HSET',KEYS[1],'v','1','epoch',ARGV[1],'tail','0-0','floor','0-0','bytes','0','catalog_revision','0')
  epoch = ARGV[1]
end
valid()
return redis.call('HGETALL',KEYS[1])
"""
)
_CAPTURE = (
    _COMMON
    + """
valid()
expect(KEYS[4],'hash')
local result = {}
local selected = ARGV[1]
local agents = redis.call('HGETALL',KEYS[3])
for i=1,#agents,2 do
  if selected == '' or agents[i] == selected then table.insert(result,agents[i]); table.insert(result,agents[i+1]) end
end
return {redis.call('HGETALL',KEYS[1]),result,redis.call('HGETALL',KEYS[4])}
"""
)
_READ = (
    _COMMON
    + """
if not epoch and redis.call('EXISTS',KEYS[1],KEYS[2],KEYS[3]) == 0 then return {'reset'} end
valid()
if epoch ~= ARGV[1] then return {'reset'} end
local floor = redis.call('HGET',KEYS[1],'floor')
if less(ARGV[4],floor) then return {'overflow'} end
local rows = redis.call('XRANGE',KEYS[2],'('..ARGV[4],ARGV[5],'COUNT',128)
local bytes = 0
local result = {}
for _,row in ipairs(rows) do
  local size = 0
  for i=2,#row[2],2 do size=size+#row[2][i] end
  if #result > 0 and bytes + size > 1048576 then break end
  bytes=bytes+size
  table.insert(result,row)
end
return {'ok', redis.call('HGET',KEYS[1],'tail'),result}
"""
)
_WRITE = (
    _COMMON
    + """
valid()
expect(KEYS[4],'hash'); expect(KEYS[5],'hash'); expect(KEYS[6],'hash')
local op = cjson.decode(ARGV[4])
if epoch ~= op.epoch then return {'reset'} end
if redis.call('HGET',KEYS[4],'token') ~= op.token then return {'lease'} end
local raw = redis.call('HGET',KEYS[3],op.agent)
local agent = raw and cjson.decode(raw) or {v=1,revision=0,status='incomplete',floor='0-0'}
if agent.v ~= 1 then error('protocol_error: origin version') end
if agent.last_id == op.id then
  if agent.last_digest ~= ARGV[5] then error('protocol_error: conflicting operation') end
  return {'ok',agent.last_result}
end
if op.kind == 'event' then
  if agent.token ~= op.token or agent.generation ~= op.generation or agent.status ~= 'complete' then return {'recover'} end
  if agent.source_epoch ~= op.source_epoch then return {'recover'} end
  if op.source <= agent.source then return {'ok',agent.last_result or '0-0'} end
  if agent.source ~= op.prior then return {'recover'} end
  if #op.data + #op.agent + 133 > 1048576 or op.count > 10000 or op.bytes > 16777216 or cjson.decode(op.data).v ~= 1 then error('protocol_error: publication bounds') end
  if redis.call('HEXISTS',KEYS[5],'["manifest"]') == 0 then error('protocol_error: missing generation') end
elseif op.kind == 'recover' then
  if agent.token ~= op.token or agent.recovery ~= op.recovery or agent.staging ~= op.generation then return {'recover'} end
  local manifest = redis.call('HGET',KEYS[5],'["manifest"]')
  if not manifest then return {'recover'} end
  local m = cjson.decode(manifest)
  if m.digest ~= op.snapshot_digest or m.sealed ~= true or m.source ~= op.source or redis.call('HLEN',KEYS[5]) ~= m.count + 1 then return {'recover'} end
elseif op.kind ~= 'incomplete' then error('protocol_error: operation') end
-- Decode and validate the entire bounded mutation before the first shared write.
local fields = {}
for key,value in pairs(op.updates or {}) do
  local decoded = cjson.decode(value)
  if decoded.v ~= 1 then error('protocol_error: entity version') end
  fields[key]=value
end
local old_generation = agent.generation
if op.kind == 'recover' and old_generation and old_generation ~= op.old_generation then return {'recover'} end
redis.call('HSET',KEYS[1],'pending',op.id)
local sid
if op.kind == 'incomplete' then
  if agent.status ~= 'incomplete' or agent.token ~= op.token or agent.reason ~= op.reason or not raw then
    sid=append('incomplete',op.agent,cjson.encode({v=1,reason=op.reason}))
    agent.revision=agent.revision+1
  else sid=redis.call('HGET',KEYS[1],'tail') end
  agent.status='incomplete'; agent.reason=op.reason; agent.token=op.token
  agent.recovery=op.recovery; agent.staging=op.generation
elseif op.kind == 'recover' then
  sid=append('recovered',op.agent,cjson.encode({v=1,recovery=op.recovery}))
  local m=cjson.decode(redis.call('HGET',KEYS[5],'["manifest"]'))
  m.baseline=sid
  redis.call('HSET',KEYS[5],'["manifest"]',cjson.encode(m))
  redis.call('PERSIST',KEYS[5])
  if old_generation and old_generation ~= op.generation then redis.call('UNLINK',KEYS[6]) end
  agent.generation=op.generation; agent.staging=nil; agent.status='complete'; agent.reason=nil
  agent.source=op.source; agent.source_epoch=op.source_epoch; agent.floor=sid; agent.revision=agent.revision+1
else
  sid=append('event',op.agent,op.data)
  for key,value in pairs(fields) do
    redis.call('HSET',KEYS[5],key,'{"v":1,"entity":'..value..',"delivery":"'..sid..'"}')
  end
  for _,key in ipairs(op.removed or {}) do redis.call('HDEL',KEYS[5],key) end
  if op.structural then
    local m=cjson.decode(redis.call('HGET',KEYS[5],'["manifest"]'))
    m.count=op.count; m.bytes=op.bytes
    redis.call('HSET',KEYS[5],'["manifest"]',cjson.encode(m))
    agent.revision=agent.revision+1
  end
  if op.evicted then agent.floor=sid end
  agent.source=op.source
end
if not raw then redis.call('HINCRBY',KEYS[1],'catalog_revision',1) end
agent.last_id=op.id; agent.last_digest=ARGV[5]; agent.last_result=sid
redis.call('HSET',KEYS[3],op.agent,cjson.encode(agent))
redis.call('HDEL',KEYS[1],'pending')
return {'ok',sid}
"""
)
_STAGE = (
    _COMMON
    + """
valid()
expect(KEYS[4],'hash'); expect(KEYS[5],'hash'); expect(KEYS[6],'hash')
local op=cjson.decode(ARGV[4])
if epoch ~= op.epoch then return {'reset'} end
if redis.call('HGET',KEYS[4],'token') ~= op.token then return {'lease'} end
local raw=redis.call('HGET',KEYS[3],op.agent)
if not raw then return {'recover'} end
local agent=cjson.decode(raw)
if agent.recovery ~= op.recovery or agent.staging ~= op.generation then return {'recover'} end
if #ARGV[4] > 16777216 then return {'limit'} end
local fields={}
local count=0
for key,value in pairs(op.entities) do
  local decoded=cjson.decode(value)
  if decoded.v ~= 1 then error('protocol_error: entity version') end
  fields[key]=value; count=count+1
end
if count > 10000 then return {'limit'} end
-- Only staging is touched. A retry recreates all fields after interruption/expiry.
redis.call('DEL',KEYS[5])
for key,value in pairs(fields) do redis.call('HSET',KEYS[5],key,value) end
redis.call('HSET',KEYS[5],'["manifest"]',cjson.encode({v=1,source=op.source,digest=op.digest,count=count,bytes=#ARGV[4],sealed=true,baseline='0-0'}))
redis.call('EXPIRE',KEYS[5],60)
return {'ok','0-0'}
"""
)

_ABANDON = (
    _COMMON
    + """
valid()
expect(KEYS[4],'hash'); expect(KEYS[5],'hash')
if redis.call('HGET',KEYS[4],'token') ~= ARGV[1] then return {'lease'} end
local raw = redis.call('HGET',KEYS[3],ARGV[2])
local agent = raw and cjson.decode(raw) or {}
if raw and agent.v ~= 1 then error('protocol_error: origin version') end
if agent.generation ~= ARGV[3] then redis.call('UNLINK',KEYS[5]) end
return {'ok','0-0'}
"""
)


def _hash(values: Any) -> dict[str, str]:
    if isinstance(values, dict):
        return values
    return dict(zip(values[::2], values[1::2], strict=True))


class EventBackend:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend

    async def _eval(self, script: str, keys: list[str], args: list[Any]) -> Any:
        try:
            return await self.backend._eval(script, keys, args)
        except MessagingError as exc:
            from .errors import BackendUnavailable

            if isinstance(exc, BackendUnavailable):
                raise
            raise EventProtocolError(str(exc)) from exc

    async def _command(self, command: str, *args: Any) -> Any:
        try:
            return await self.backend._call(command, *args)
        except MessagingError as exc:
            from .errors import BackendUnavailable

            if isinstance(exc, BackendUnavailable):
                raise
            raise EventProtocolError("Invalid event dataset") from exc

    async def initialize(self) -> dict[str, str]:
        return _hash(await self._eval(_INIT, [META, STREAM, AGENTS], [uuid4().hex]))

    async def capture(
        self, agent: str | None = None
    ) -> tuple[dict[str, str], dict[str, Any], dict[str, str]]:
        meta, agents, participants = await self._eval(
            _CAPTURE, [META, STREAM, AGENTS, PARTICIPANTS], [agent or ""]
        )
        try:
            origins = {key: json.loads(value) for key, value in _hash(agents).items()}
            for value in origins.values():
                if value.get("v") != 1:
                    raise ValueError("Origin schema version")
                stream_id(value.get("floor", "0-0"))
            return _hash(meta), origins, _hash(participants)
        except (ValueError, TypeError, AttributeError, MessagingError) as exc:
            raise EventProtocolError("Invalid origin metadata") from exc

    async def projection(
        self, agent: str, generation: str
    ) -> dict[str, dict[str, Any]]:
        key = generation_key(agent, generation)
        result: dict[str, dict[str, Any]] = {}
        cursor = 0
        size = 0
        while True:
            cursor, page = await self._command("HSCAN", key, cursor, "COUNT", 128)
            for name, value in page.items():
                if name not in result:
                    size += len(name.encode()) + len(value.encode())
                if size > MAX_BYTES or len(result) > MAX_ENTITIES:
                    raise EventProtocolError("Oversized event generation")
                try:
                    data = json.loads(value)
                    if not isinstance(data, dict) or data.get("v") != 1:
                        raise ValueError("Unsupported event entity version")
                    if "entity" in data:
                        entity = data["entity"]
                        if not isinstance(entity, dict) or entity.get("v") != 1:
                            raise ValueError("Unsupported event entity version")
                        data = {**entity, "delivery": data["delivery"]}
                    if name == MANIFEST:
                        stream_id(data["baseline"])
                except (ValueError, TypeError, KeyError, MessagingError) as exc:
                    raise EventProtocolError("Invalid stored event entity") from exc
                result[name] = data
            if int(cursor) == 0:
                return result

    async def online_token(self, agent: str) -> str | None:
        return await self._command("HGET", online_key(agent), "token")

    async def commit(self, op: dict[str, Any]) -> str:
        op = {
            **op,
            "old_key": generation_key(
                op["agent"], op.get("old_generation") or op["generation"]
            ),
        }
        raw = encode(op)
        keys = [
            META,
            STREAM,
            AGENTS,
            online_key(op["agent"]),
            generation_key(op["agent"], op["generation"]),
            op.get("old_key") or generation_key(op["agent"], op["generation"]),
        ]
        response = await self._eval(
            _WRITE,
            keys,
            [
                "",
                MAX_STREAM_EVENTS,
                MAX_STREAM_BYTES,
                raw,
                sha256(raw.encode()).hexdigest(),
            ],
        )
        return self._result(response)

    async def stage(self, op: dict[str, Any]) -> None:
        raw = encode(op)
        if len(raw.encode()) > MAX_BYTES:
            from toolang.execution.errors import SnapshotLimitError

            raise SnapshotLimitError("Snapshot upload exceeds budget")
        key = generation_key(op["agent"], op["generation"])
        response = await self._eval(
            _STAGE,
            [META, STREAM, AGENTS, online_key(op["agent"]), key, key],
            ["", MAX_STREAM_EVENTS, MAX_STREAM_BYTES, raw],
        )
        self._result(response)

    async def abandon(self, agent: str, generation: str, *, token: str) -> None:
        # Cleanup is a fenced write too. Check the lease and current generation
        # atomically so ownership changes or activation cannot race the deletion.
        response = await self._eval(
            _ABANDON,
            [
                META,
                STREAM,
                AGENTS,
                online_key(agent),
                generation_key(agent, generation),
            ],
            [token, agent, generation],
        )
        self._result(response)

    @staticmethod
    def _result(response: Any) -> str:
        if response[0] == "ok":
            return response[1]
        if response[0] in {"reset", "recover", "lease"}:
            raise EventRecoveryRequired(response[0])
        raise EventProtocolError(str(response))

    async def read(
        self, cursor: HubCursor, until: str = "+"
    ) -> tuple[str, list[tuple[str, dict[str, str]]]]:
        response = await self._eval(
            _READ,
            [META, STREAM, AGENTS],
            [
                cursor.epoch,
                MAX_STREAM_EVENTS,
                MAX_STREAM_BYTES,
                cursor.stream_id,
                until,
            ],
        )
        if response[0] != "ok":
            raise EventRecoveryRequired(response[0])
        return response[1], [(sid, _hash(values)) for sid, values in response[2]]

    async def wait(self, after: str) -> None:
        await self._command(
            "XREAD", "BLOCK", 1000, "COUNT", 1, "STREAMS", STREAM, after
        )
