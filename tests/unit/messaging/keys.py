"""Storage addresses for the explicit root shared by offline messaging fixtures."""

from toolang.common.instance_ids import area_id
from toolang.teaming.backend.valkey.keys import Keys

ROOT = "/test/toolang"
KEYS = Keys(area_id(ROOT))
PREFIX = KEYS.prefix
TEAM_MEMBERS = KEYS.team_members
PRESENCE = KEYS.presence
TEAM_EVENTS = KEYS.team_events
CONVOS = KEYS.convos
SCHEMA = KEYS.schema
GC_ALLOCATOR = KEYS.gc_allocator
STATS = KEYS.stats
SYSTEM = KEYS.system
ROSTER = KEYS.roster
EVENT_META = KEYS.event_meta
EVENT_STREAM = KEYS.event_stream
EVENT_AGENTS = KEYS.event_agents
convo_key = KEYS.convo_key
name_key = KEYS.name_key
generation_key = KEYS.generation_key
talk_key = KEYS.talk_key
