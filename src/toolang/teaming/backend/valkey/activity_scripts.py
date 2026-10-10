"""Atomic activity publication, fenced by lease and observation boundary."""

from .lua import LEASE_CHECK

SAVE = (
    LEASE_CHECK
    + """
local lease=current_lease(KEYS[1],KEYS[3],ARGV[4])
if not lease or lease.token~=ARGV[1] then return 0 end
local old=redis.call('HGET',KEYS[2],ARGV[2])
if old then
  local a=cjson.decode(old); local b=cjson.decode(ARGV[3])
  if a.query==b.query and a.pages[1].session==b.pages[1].session then
    if a.pages[1].revision>b.pages[1].revision or
      (a.pages[1].revision==b.pages[1].revision and a.pages[1].observed>b.pages[1].observed)
    then return 0 end
  end
end
redis.call('HSET',KEYS[2],ARGV[2],ARGV[3]); return 1

"""
)
