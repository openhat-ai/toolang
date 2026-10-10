"""Shared atomic guards; protocol record validation lives in Python."""

from ...types import MAX_SAFE_INTEGER

# Also used by execution/activity publication, with explicit team/presence keys.
LEASE_CHECK = (
    f"local max_safe_integer={MAX_SAFE_INTEGER}\n"
    + """
local function current_lease(team, presence, member)
  local raw=redis.call('HGET',team,member)
  local score=redis.call('ZSCORE',presence,member)
  if not raw then
    if score then error('integrity: orphan presence') end
    return nil
  end
  -- The caller fences a Python-validated team record before entering this function.
  local lease=cjson.decode(raw).lease
  if (score and lease==cjson.null) or (not score and lease~=cjson.null) then
    error('integrity: inconsistent presence')
  end
  if score then
    local deadline=tonumber(score)
    if not deadline or deadline<0 or deadline>max_safe_integer or deadline~=math.floor(deadline) then error('integrity: invalid presence deadline') end
    local now=redis.call('TIME')
    if tonumber(score)>tonumber(now[1])*1000+math.floor(tonumber(now[2])/1000) then return lease end
  end
  return nil
end
"""
)

# The last argument carries the exact Python-validated team record. Run this
# guard before any write, including event metadata and generation cleanup.
TEAM_GUARD = """
local checked=cjson.decode(ARGV[#ARGV])
local raw=redis.call('HGET',KEYS[checked.key],checked.member)
if (raw or cjson.null)~=checked.raw then error('snapshot_changed') end
"""

# Compare decimal components without losing precision through Lua doubles.
STREAM_ORDER = """
local function less(a,b)
  local am,as=string.match(a,'^(%d+)%-(%d+)$')
  local bm,bs=string.match(b,'^(%d+)%-(%d+)$')
  if not am or not bm then error('integrity: invalid stream ID') end
  if #am~=#bm then return #am<#bm end
  if am~=bm then return am<bm end
  if #as~=#bs then return #as<#bs end
  return as<bs
end
"""
