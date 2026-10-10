"""Conversation ID codecs, independent of names and storage."""

import hashlib
import json

from toolang.common.ids import encode_short_id, scramble_id
from .schemas import direct_pair

GC_EPOCH_SECONDS = 1767225600
GC_LIMIT = 1 << 20
_GC_KEY = hashlib.blake2s(b"toolang:conversation:gc:v1").digest()


def dm_id(a: str, b: str) -> str:
    pair = json.loads(direct_pair(a, b))
    data = json.dumps(
        ["toolang:dm:v1", *pair], ensure_ascii=False, separators=(",", ":")
    )
    value = int.from_bytes(hashlib.sha256(data.encode()).digest()[:5], "big")
    return "dm_" + encode_short_id(value)


def gc_id(tick: int, sequence: int) -> str:
    if not 0 <= tick < GC_LIMIT or not 0 <= sequence < GC_LIMIT:
        raise ValueError("Conversation ID allocation exhausted")
    return "gc_" + encode_short_id(
        scramble_id((tick << 20) | sequence, width=8, key=_GC_KEY)
    )
