"""Atomic conversation, team, and presence operations owned by Backend.

The first nine keys always follow keys.BASE_KEYS. Extra keys are explicitly
provided by the caller; all validation precedes mutations because Lua cannot
roll back an error after a write.
"""

# Also used by execution/activity publication, with explicit team/presence keys.
LEASE_CHECK = """
local function current_lease(team, presence, member)
  local raw=redis.call('HGET',team,member)
  local score=redis.call('ZSCORE',presence,member)
  if not raw then
    if score then error('integrity: orphan presence') end
    return nil
  end
  local ok,record=pcall(cjson.decode,raw)
  if not ok or type(record)~='table' then error('integrity: invalid team JSON') end
  local lease=record.lease
  if lease==nil then error('integrity: missing lease field') end
  if (score and lease==cjson.null) or (not score and lease~=cjson.null) then
    error('integrity: inconsistent presence')
  end
  if score then
    local deadline=tonumber(score)
    if not deadline or deadline<0 or deadline>9007199254740990 or deadline~=math.floor(deadline) then error('integrity: invalid presence deadline') end
    if type(lease)~='table' or type(lease.token)~='string' or lease.token=='' or type(lease.endpoint)~='string' then error('integrity: invalid lease') end
    for key,_ in pairs(lease) do if key~='token' and key~='endpoint' then error('integrity: unknown lease field') end end
    local now=redis.call('TIME')
    if tonumber(score)>tonumber(now[1])*1000+math.floor(tonumber(now[2])/1000) then return lease end
  end
  return nil
end
"""

COMMON = (
    LEASE_CHECK
    + """
local function fail(message) error('integrity: '..message) end
local function expect(key,kind)
  local actual=redis.call('TYPE',key).ok
  if actual~='none' and actual~=kind then fail('invalid key type') end
end
local function hash(values)
  local result={}
  for i=1,#values,2 do result[values[i]]=values[i+1] end
  return result
end
local clock=redis.call('TIME')
local now=tonumber(clock[1])*1000+math.floor(tonumber(clock[2])/1000)
local function timestamp()
  local seconds=math.floor(now/1000)
  local days=math.floor(seconds/86400)+719468
  local era=math.floor(days/146097)
  local doe=days-era*146097
  local yoe=math.floor((doe-math.floor(doe/1460)+math.floor(doe/36524)-math.floor(doe/146096))/365)
  local year=yoe+era*400
  local doy=doe-(365*yoe+math.floor(yoe/4)-math.floor(yoe/100))
  local mp=math.floor((5*doy+2)/153)
  local day=doy-math.floor((153*mp+2)/5)+1
  local month=mp+(mp<10 and 3 or -9)
  if month<=2 then year=year+1 end
  return string.format('%04d-%02d-%02dT%02d:%02d:%02d.%03dZ',year,month,day,math.floor(seconds/3600)%24,math.floor(seconds/60)%60,seconds%60,now%1000)
end
local epoch=nil
local function integer(value,minimum,maximum)
  -- HINCRBY requires canonical decimal text; tonumber alone accepts 0.0/0e0.
  if type(value)=='string' and value~='0' and not string.match(value,'^%-?[1-9][0-9]*$') then fail('invalid counter encoding') end
  local number=tonumber(value)
  if not number or number~=math.floor(number) or number<minimum or number>maximum then fail('invalid counter') end
  return number
end
local function fields(record,allowed)
  if type(record)~='table' then fail('invalid record') end
  local count=0
  for key,_ in pairs(record) do
    if not allowed[key] then fail('unknown record field') end
    count=count+1
  end
  local expected=0
  for key,_ in pairs(allowed) do
    expected=expected+1
    if record[key]==nil then fail('missing record field') end
  end
  if count~=expected then fail('invalid record') end
end
local function decode(raw)
  local ok,value=pcall(cjson.decode,raw)
  if not ok or type(value)~='table' then fail('invalid record JSON') end
  return value
end
local function stream_capacity(info)
  if string.match(info['last-generated-id'],'^(%d+)%-')=='18446744073709551615' then fail('stream ID exhausted') end
  integer(info['entries-added'],0,9007199254740989)
end
local function validate_event(value)
  fields(value,{v=true,epoch=true,type=true,conversation=true,actor=true,payload=true})
  if value.v~=1 or type(value.epoch)~='string' or #value.epoch~=32 or not string.match(value.epoch,'^[0-9a-f]+$') or type(value.payload)~='table' then fail('invalid team event') end
  local kind=value.type
  local payload=value.payload
  if kind=='stream.initialized' then fields(payload,{})
  elseif kind=='team.member_added' or kind=='team.member_removed' or kind=='conversation.member_added' or kind=='conversation.member_removed' then fields(payload,{member=true})
  elseif kind=='presence.online' then fields(payload,{member=true,effective_at_ms=true,observed_at_ms=true})
  elseif kind=='presence.offline' then
    fields(payload,{member=true,effective_at_ms=true,observed_at_ms=true,reason=true})
    if payload.reason~='expired' and payload.reason~='released' then fail('invalid offline reason') end
  elseif kind=='presence.updated' then
    fields(payload,{member=true,effective_at_ms=true,observed_at_ms=true,changed=true})
    if type(payload.changed)~='table' or #payload.changed~=1 or payload.changed[1]~='endpoint' then fail('invalid presence changes') end
  else fail('invalid team event type') end
  if kind~='stream.initialized' and type(payload.member)~='string' then fail('invalid event member') end
  if value.actor~=cjson.null and type(value.actor)~='string' then fail('invalid event actor') end
  if string.sub(kind,1,13)=='conversation.' then
    if type(value.conversation)~='string' or (string.sub(value.conversation,1,3)~='dm_' and string.sub(value.conversation,1,3)~='gc_') or not string.match(string.sub(value.conversation,4),'^[0-9a-hjkmnp-tv-z]+$') or #value.conversation~=11 then fail('invalid event conversation') end
  elseif value.conversation~=cjson.null then fail('unexpected event conversation') end
  if string.sub(kind,1,9)=='presence.' then
    integer(payload.effective_at_ms,0,9007199254740990)
    integer(payload.observed_at_ms,0,9007199254740990)
  end
end
local function feed()
  expect(KEYS[7],'stream')
  if redis.call('EXISTS',KEYS[7])==0 then fail('missing team events') end
  local info=hash(redis.call('XINFO','STREAM',KEYS[7]))
  local row=info['last-entry']
  if not row or row==false or row[1]~=info['last-generated-id'] then fail('inconsistent event tail') end
  local fields=hash(row[2])
  local event=decode(fields.data or '{}')
  validate_event(event)
  stream_capacity(info)
  epoch=event.epoch
  return info
end
local function valid()
  expect(KEYS[1],'string'); expect(KEYS[2],'hash'); expect(KEYS[3],'zset')
  expect(KEYS[4],'hash'); expect(KEYS[5],'hash'); expect(KEYS[6],'hash')
  expect(KEYS[8],'hash'); expect(KEYS[9],'hash')
  if redis.call('GET',KEYS[1])~='2' then fail('unsupported conversation schema; use a fresh dataset') end
  for _,field in ipairs({'dm_count','gc_count','messages_total'}) do integer(redis.call('HGET',KEYS[5],field),0,9007199254740990) end
  local dm=tonumber(redis.call('HGET',KEYS[5],'dm_count'))
  local gc=tonumber(redis.call('HGET',KEYS[5],'gc_count'))
  if dm+gc~=redis.call('HLEN',KEYS[4]) then fail('inconsistent conversation totals') end
  local system=redis.call('HGET',KEYS[8],'all')
  if gc>0 and (not system or not redis.call('HGET',KEYS[4],system)) then fail('missing system conversation') end
  local tick=integer(redis.call('HGET',KEYS[6],'last_tick'),-1,1048575)
  local seq=integer(redis.call('HGET',KEYS[6],'last_seq'),-1,1048575)
  if (tick==-1)~=(seq==-1) then fail('invalid allocator') end
  return feed()
end
local function capacity(field)
  integer(redis.call('HGET',KEYS[5],field),0,9007199254740989)
end
local function event(kind,member,conversation,actor,payload)
  payload=payload or {member=member}
  redis.call('XADD',KEYS[7],'MAXLEN','~',100000,'*','data',cjson.encode({v=1,epoch=epoch,type=kind,conversation=conversation or cjson.null,actor=actor or cjson.null,payload=payload}))
end
local function member_record(member,required)
  local raw=redis.call('HGET',KEYS[2],member)
  if not raw then
    if redis.call('ZSCORE',KEYS[3],member) then fail('orphan presence') end
    if required then error('Unknown participant: '..member) end
    return nil
  end
  local record=decode(raw)
  fields(record,{display_name=true,owner=true,created_at=true,lease=true})
  if type(record.display_name)~='string' or type(record.created_at)~='string' then fail('invalid team record') end
  if string.sub(member,1,6)=='human:' then
    if record.owner~=cjson.null or record.lease~=cjson.null then fail('invalid human record') end
  elseif type(record.owner)~='string' or string.sub(record.owner,1,6)~='human:' then fail('invalid agent owner') end
  current_lease(KEYS[2],KEYS[3],member)
  return record
end
local function authorize(actor,token)
  member_record(actor,true)
  if string.sub(actor,1,6)=='agent:' then
    local lease=current_lease(KEYS[2],KEYS[3],actor)
    if not lease or lease.token~=token then error('Agent lease lost') end
  end
end
local function record(id)
  local raw=redis.call('HGET',KEYS[4],id)
  if not raw then return nil end
  local value=decode(raw)
  fields(value,{id=true,kind=true,name=true,created_by=true,created_at=true,updated_at=true,revision=true})
  if value.id~=id or (value.kind~='dm' and value.kind~='gc') or string.sub(id,1,3)~=value.kind..'_' then fail('invalid conversation record') end
  if value.name~=cjson.null and type(value.name)~='string' then fail('invalid name') end
  if value.created_by==cjson.null then
    if redis.call('HGET',KEYS[8],'all')~=id then fail('missing conversation creator') end
  elseif type(value.created_by)~='string' or (string.sub(value.created_by,1,6)~='agent:' and string.sub(value.created_by,1,6)~='human:') then fail('invalid creator') end
  if type(value.created_at)~='string' or type(value.updated_at)~='string' or not string.match(value.created_at,'^%d%d%d%d%-%d%d%-%d%dT%d%d:%d%d:%d%d%.%d%d%dZ$') or not string.match(value.updated_at,'^%d%d%d%d%-%d%d%-%d%dT%d%d:%d%d:%d%d%.%d%d%dZ$') then fail('invalid timestamps') end
  if type(value.revision)~='number' then fail('invalid revision') end
  integer(value.revision,1,9007199254740990)
  return value
end
local function readable(id,actor,members)
  expect(members,'set')
  local value=record(id)
  if not value then error('Unknown conversation: '..id) end
  if value.kind=='dm' and redis.call('SCARD',members)~=2 then fail('invalid DM membership') end
  if string.sub(actor,1,6)=='agent:' and redis.call('SISMEMBER',members,actor)==0 then error('Agent is not a member of this conversation') end
  return value
end
local function same_pair(members,pair)
  if #pair~=2 or redis.call('SCARD',members)~=2 or redis.call('SISMEMBER',members,pair[1])~=1 or redis.call('SISMEMBER',members,pair[2])~=1 then error('DM collision or corrupt membership') end
end
local function expire(member,actor,reason)
  local raw=redis.call('HGET',KEYS[2],member)
  local score=redis.call('ZSCORE',KEYS[3],member)
  if not score then return false end
  local value=decode(raw)
  value.lease=cjson.null
  redis.call('HSET',KEYS[2],member,cjson.encode(value))
  redis.call('ZREM',KEYS[3],member)
  event('presence.offline',member,nil,actor,{member=member,effective_at_ms=reason=='expired' and tonumber(score) or now,observed_at_ms=now,reason=reason})
  return true
end
"""
)

INITIALIZE = (
    COMMON
    + """
local fresh=redis.call('EXISTS',KEYS[1])==0
expect(KEYS[1],'string')
local cursor='0'
repeat
  local page=redis.call('SCAN',cursor,'MATCH',ARGV[2]..':*','COUNT',128)
  cursor=page[1]
  for _,key in ipairs(page[2]) do
    if string.find(key,ARGV[2]..':msg:',1,true)==1 or key==ARGV[2]..':participants' or key==ARGV[2]..':last_seen' or string.find(key,ARGV[2]..':agent:',1,true)==1 then fail('unsupported legacy dataset') end
    if fresh and string.find(key,ARGV[2]..':convo:',1,true)==1 then fail('partial conversation dataset') end
  end
until cursor=='0'
if not redis.call('GET',KEYS[1]) then
  if ARGV[3]=='1' then fail('missing initialized conversation schema') end
  for i=2,8 do if redis.call('EXISTS',KEYS[i])~=0 then fail('partial conversation dataset') end end
  expect(KEYS[9],'hash')
  if redis.call('HLEN',KEYS[9])>0 then fail('partial roster dataset') end
  redis.call('SET',KEYS[1],'2')
  redis.call('HSET',KEYS[5],'dm_count',0,'gc_count',0,'messages_total',0)
  redis.call('HSET',KEYS[6],'last_tick',-1,'last_seq',-1)
  epoch=ARGV[1]
  event('stream.initialized',nil,nil,nil,{})
end
valid()
return 1
"""
)

RESERVE = (
    COMMON
    + """
valid()
local op=cjson.decode(ARGV[1])
if op.actor then authorize(op.actor,op.token) end
local tick=math.floor((tonumber(clock[1])-1767225600)/3600)
if tick<0 then error('Clock precedes conversation epoch') end
local last=tonumber(redis.call('HGET',KEYS[6],'last_tick'))
local seq=0
if tick<=last then tick=last; seq=tonumber(redis.call('HGET',KEYS[6],'last_seq'))+1 end
if tick>=1048576 or seq>=1048576 then error('Conversation ID allocation exhausted') end
redis.call('HSET',KEYS[6],'last_tick',tick,'last_seq',seq)
return {tick,seq}
"""
)

CONVERSATION = (
    COMMON
    + """
valid()
local op=cjson.decode(ARGV[1])
expect(KEYS[10],'set'); expect(KEYS[11],'stream'); expect(KEYS[12],'set')
local value=record(op.id)
local pair=op.participants or {}
if value and #pair>0 then
  if value.kind~='dm' then error('DM collision or corrupt kind') end
  same_pair(KEYS[10],pair)
end
if op.action=='lookup' then
  if not value then return false end
  readable(op.id,op.actor,KEYS[10])
  return {cjson.encode(value),redis.call('SMEMBERS',KEYS[10])}
end
if op.system and redis.call('HGET',KEYS[8],'all') then return redis.call('HGET',KEYS[8],'all') end
if not op.system then authorize(op.actor,op.token) end
if not value then
  if op.action=='send' and (op.kind~='dm' or #pair~=2) then error('Unknown conversation: '..op.id) end
  if redis.call('EXISTS',KEYS[10],KEYS[11])~=0 then
    if op.kind=='gc' then return false end
    error('DM collision or orphaned conversation')
  end
  if op.kind=='dm' then
    if #pair~=2 or pair[1]==pair[2] or (op.actor~=pair[1] and op.actor~=pair[2]) then error('Conversation is read-only for nonparticipants') end
  elseif not op.system then pair={op.actor} else pair={} end
  for _,member in ipairs(pair) do member_record(member,true) end
  capacity(op.kind..'_count')
elseif op.action=='create' then
  if op.kind=='gc' then return false end
  return op.id
end
if op.action=='send' then
  if value and redis.call('SISMEMBER',KEYS[10],op.actor)~=1 then error('Conversation is read-only for nonparticipants') end
  capacity('messages_total')
  if redis.call('EXISTS',KEYS[11])==1 then stream_capacity(hash(redis.call('XINFO','STREAM',KEYS[11]))) end
end
-- No fallible validation after this point.
if not value then
  local stamp=timestamp()
  value={id=op.id,kind=op.kind,name=op.name or cjson.null,created_by=op.system and cjson.null or op.actor,created_at=stamp,updated_at=stamp,revision=1}
  redis.call('HSET',KEYS[4],op.id,cjson.encode(value))
  redis.call('HINCRBY',KEYS[5],op.kind..'_count',1)
  for _,member in ipairs(pair) do
    redis.call('SADD',KEYS[10],member)
    event('conversation.member_added',member,op.id,op.actor)
  end
  if op.name and op.name~=cjson.null then redis.call('SADD',KEYS[12],op.id) end
  if op.system then redis.call('HSET',KEYS[8],'all',op.id) end
end
if op.action=='send' then
  local sid=redis.call('XADD',KEYS[11],'MAXLEN','~',10000,'*','data',op.data)
  redis.call('HINCRBY',KEYS[5],'messages_total',1)
  return sid
end
return op.id
"""
)

EDIT = (
    COMMON
    + """
valid()
local op=cjson.decode(ARGV[1])
expect(KEYS[10],'set'); expect(KEYS[11],'set'); expect(KEYS[12],'set')
authorize(op.actor,op.token)
local value=record(op.id)
if not value then error('Unknown conversation: '..op.id) end
if op.action=='rename' then
  readable(op.id,op.actor,KEYS[10])
  if redis.call('SISMEMBER',KEYS[10],op.actor)~=1 then error('Conversation is read-only for nonparticipants') end
  if value.revision~=op.revision or value.name~=op.previous then error('Conversation revision conflict; reload before renaming') end
  if value.name==op.name then return 0 end
  if value.revision>=9007199254740990 then fail('revision exhausted') end
  value.name=op.name; value.updated_at=timestamp(); value.revision=value.revision+1
  if op.previous~=cjson.null then redis.call('SREM',KEYS[11],op.id) end
  if op.name~=cjson.null then redis.call('SADD',KEYS[12],op.id) end
  redis.call('HSET',KEYS[4],op.id,cjson.encode(value))
  return 1
end
if value.kind~='gc' or redis.call('HGET',KEYS[8],'all')==op.id then error('Cannot edit DM/system membership') end
local changed=redis.call(op.join and 'SADD' or 'SREM',KEYS[10],op.actor)
if changed==1 then event(op.join and 'conversation.member_added' or 'conversation.member_removed',op.actor,op.id,op.actor) end
return changed
"""
)

PRESENCE = (
    COMMON
    + """
valid()
local op=cjson.decode(ARGV[1])
local member=op.agent
local value=member_record(member,false)
local score=redis.call('ZSCORE',KEYS[3],member)
local lease=value and value.lease or cjson.null
local live=score and tonumber(score)>now
if op.action=='inspect' then
  return cjson.encode({deadline=score and tonumber(score) or cjson.null,lease=live and lease or cjson.null})
end
if op.action=='expire' then
  if not score or live then return 0 end
  expire(member,nil,'expired'); return 1
end
if op.action=='renew' or op.action=='release' then
  if not live or lease.token~=op.token then return 0 end
  if op.action=='release' then expire(member,member,'released')
  else redis.call('ZADD',KEYS[3],math.max(tonumber(score),now+15000),member) end
  return 1
end
expect(KEYS[10],'set')
local human=member_record(op.owner,false)
local system=redis.call('HGET',KEYS[8],'all')
if not system or system~=op.system then fail('missing system conversation') end
local system_record=record(system)
if not system_record or system_record.kind~='gc' then fail('invalid system conversation') end
if member~=op.owner and value and value.owner~=op.owner then error('Agent owner mismatch') end
if live and lease.token~=op.token then error('Agent already online') end
if op.root~='' then
  local managed=redis.call('HGET',KEYS[9],member)
  if not managed or cjson.decode(managed).root~=op.root then error('Agent identity changed') end
end
-- All validation precedes registration and expiry settlement.
if not human then
  redis.call('HSET',KEYS[2],op.owner,cjson.encode({display_name=string.sub(op.owner,7),owner=cjson.null,created_at=timestamp(),lease=cjson.null}))
  event('team.member_added',op.owner,nil,op.owner)
end
if redis.call('SADD',KEYS[10],op.owner)==1 then event('conversation.member_added',op.owner,system,op.owner) end
if member==op.owner then return 1 end
if not value then
  value={display_name=string.sub(member,7),owner=op.owner,created_at=timestamp(),lease=cjson.null}
  event('team.member_added',member,nil,op.owner)
end
if score and not live then expire(member,nil,'expired') end
if redis.call('SADD',KEYS[10],member)==1 then event('conversation.member_added',member,system,op.owner) end
local changed=live and lease.endpoint~=op.endpoint
value.lease={token=op.token,endpoint=op.endpoint}
redis.call('HSET',KEYS[2],member,cjson.encode(value))
redis.call('ZADD',KEYS[3],math.max(score and tonumber(score) or 0,now+15000),member)
if not live then event('presence.online',member,nil,member,{member=member,effective_at_ms=now,observed_at_ms=now})
elseif changed then event('presence.updated',member,nil,member,{member=member,effective_at_ms=now,observed_at_ms=now,changed={'endpoint'}}) end
return 1
"""
)

READ = (
    COMMON
    + """
local info=valid()
local op=cjson.decode(ARGV[1])
if op.action=='team' then
  local rows=redis.call('HGETALL',KEYS[2])
  local result={}
  for i=1,#rows,2 do
    local value=member_record(rows[i],true)
    value.lease=nil
    value.member=rows[i]
    value.deadline=tonumber(redis.call('ZSCORE',KEYS[3],rows[i])) or cjson.null
    table.insert(result,value)
  end
  return cjson.encode(result)
end
if op.action=='due' then return redis.call('ZRANGEBYSCORE',KEYS[3],'-inf',now,'LIMIT',0,128) end
if op.action=='metadata' then return {epoch,info['last-generated-id']} end
if op.action=='stats' then
  if not op.id or op.id==cjson.null then
    if string.sub(op.actor,1,6)~='human:' then error('Global statistics require human observer access') end
    local stats=hash(redis.call('HGETALL',KEYS[5]))
    for k,v in pairs(stats) do stats[k]=tonumber(v) end
    stats.conversations_total=redis.call('HLEN',KEYS[4])
    return cjson.encode(stats)
  end
  readable(op.id,op.actor,KEYS[10]); expect(KEYS[11],'stream')
  local count=0; local retained=0; local last=cjson.null
  if redis.call('EXISTS',KEYS[11])==1 then
    local stream=hash(redis.call('XINFO','STREAM',KEYS[11]))
    count=integer(stream['entries-added'],0,9007199254740990)
    retained=stream.length
    if stream['last-entry'] then last=stream['last-entry'][1] end
  end
  return cjson.encode({participants_count=redis.call('SCARD',KEYS[10]),messages_total=count,messages_retained=retained,last_message_stream_id=last})
end
if op.action=='messages' then
  readable(op.id,op.actor,KEYS[10]); expect(KEYS[11],'stream')
  return redis.call(op.reverse and 'XREVRANGE' or 'XRANGE',KEYS[11],op.start,op.finish,'COUNT',op.count)
end
return 1
"""
)

ROSTER = (
    COMMON
    + """
valid()
local op=cjson.decode(ARGV[1])
local old=redis.call('HGET',KEYS[9],op.agent) or ''
if old~=op.previous then return 0 end
local value=member_record(op.agent,false)
if value and value.owner~=op.owner then error('Agent owner mismatch') end
if op.updated=='' then
  if current_lease(KEYS[2],KEYS[3],op.agent) then return 0 end
  if redis.call('HLEN',KEYS[4])~=#op.conversations then return 0 end
  local conversations={}
  for i,id in ipairs(op.conversations) do
    local info=record(id)
    if not info then return 0 end
    expect(KEYS[9+i],'set')
    if info.kind=='gc' then table.insert(conversations,{id,KEYS[9+i]}) end
  end
  if redis.call('ZSCORE',KEYS[3],op.agent) then expire(op.agent,nil,'expired') end
  for _,row in ipairs(conversations) do
    if redis.call('SREM',row[2],op.agent)==1 then event('conversation.member_removed',op.agent,row[1],nil) end
  end
  redis.call('HDEL',KEYS[9],op.agent)
  if redis.call('HDEL',KEYS[2],op.agent)==1 then event('team.member_removed',op.agent,nil,nil) end
else
  redis.call('HSET',KEYS[9],op.agent,op.updated)
  if not value then
    redis.call('HSET',KEYS[2],op.agent,cjson.encode({display_name=string.sub(op.agent,7),owner=op.owner,created_at=timestamp(),lease=cjson.null}))
    event('team.member_added',op.agent,nil,nil)
  end
end
return 1
"""
)

REPLAY = (
    COMMON
    + """
local info=valid()
local op=cjson.decode(ARGV[1])
if op.actor then authorize(op.actor,op.token) end
local function less(a,b)
  local am,as=string.match(a,'^(%d+)%-(%d+)$')
  local bm,bs=string.match(b,'^(%d+)%-(%d+)$')
  if not am or not bm then fail('invalid event ID') end
  if #am~=#bm then return #am<#bm end
  if am~=bm then return am<bm end
  if #as~=#bs then return #as<#bs end
  return as<bs
end
if op.epoch~=epoch or less(info['last-generated-id'],op.after) then return {'resync'} end
if tonumber(info['entries-added'])>tonumber(info.length) and less(op.after,info['first-entry'][1]) then return {'resync'} end
local rows=redis.call('XRANGE',KEYS[7],'('..op.after,'+','COUNT',128)
for _,row in ipairs(rows) do
  local value=decode(hash(row[2]).data or '{}')
  validate_event(value)
  if value.epoch~=epoch then fail('invalid team event epoch') end
end
return {'ok',rows}
"""
)
