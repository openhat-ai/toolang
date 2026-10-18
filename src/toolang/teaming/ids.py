"""Conversation and agent-specific talk-thread ID codecs.

The shared 40-bit permutation preserves disjoint kind domains:
DM = 0 | pair_hash(39); GC = 1 | tick(20) | sequence(19).
Talk IDs permute the resulting suffix again with a canonical agent's key.
These pure codecs neither reserve IDs nor create conversations or threads.
"""

import hashlib
import json
from typing import Literal

from toolang.common.id_codec import (
    decode_short_id,
    derive_id_key,
    encode_short_id,
    scramble_id,
)

from .schemas import conversation_id, direct_pair, identifier

GC_EPOCH_SECONDS = 1767225600  # 2026-01-01T00:00:00Z
GC_TICK_LIMIT = 1 << 20
GC_SEQUENCE_BITS = 19
GC_SEQUENCE_LIMIT = 1 << GC_SEQUENCE_BITS

_KIND_BIT = 1 << 39
_PAYLOAD_MASK = _KIND_BIT - 1
_CONVERSATION_KEY = derive_id_key("toolang:conversation:v3")


def dm_id(a: str, b: str) -> str:
    """Derive one fixed-membership DM from its exact unordered participant pair."""
    pair = json.loads(direct_pair(a, b))
    data = json.dumps(
        ["toolang:dm:v3", *pair], ensure_ascii=False, separators=(",", ":")
    )
    value = int.from_bytes(hashlib.sha256(data.encode("utf-8")).digest()[:5], "big")
    return _conversation_id("dm", value & _PAYLOAD_MASK)


def gc_id(tick: int, sequence: int) -> str:
    """Encode one backend reservation; distinct reservations never share a suffix."""
    if not 0 <= tick < GC_TICK_LIMIT or not 0 <= sequence < GC_SEQUENCE_LIMIT:
        raise ValueError("Conversation ID allocation exhausted")
    return _conversation_id("gc", _KIND_BIT | (tick << GC_SEQUENCE_BITS) | sequence)


def talk_thread_id(conversation: str, *, agent_name: str) -> str:
    """Derive talk_<8 chars> bijectively per agent, without allocating a thread.

    Different agents still share the 40-bit output space. Cross-agent references
    must remain qualified by agent; this helper does not promise global uniqueness.
    """
    conversation_id(conversation)
    identifier(agent_name)
    key = derive_id_key("toolang:talk-thread:v1", agent_name)
    value = decode_short_id(conversation[3:])
    return "talk_" + encode_short_id(scramble_id(value, width=8, key=key))


def _conversation_id(kind: Literal["dm", "gc"], value: int) -> str:
    return (
        kind + "_" + encode_short_id(scramble_id(value, width=8, key=_CONVERSATION_KEY))
    )
