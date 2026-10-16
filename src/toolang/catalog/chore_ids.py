"""Agent-bound chore IDs with disjoint manual and automatic name domains.

Automatic filenames reserve auto_<8 lowercase Base32 characters>.md. The
shared keyed permutation encodes manual = 0 | hash(39), automatic = 1 | hash(39).
The name format determines the domain; no origin registry exists.
"""

from hashlib import sha256
import re

from toolang.common.id_codec import derive_id_key, encode_short_id, scramble_id
from toolang.common.layout import validate_agent_name

_AUTO_NAME = re.compile(r"auto_[0123456789abcdefghjkmnpqrstvwxyz]{8}\Z")
_KIND_BIT = 1 << 39


def validate_chore_name(name: str) -> None:
    """Validate a portable file/declaration name without binding an agent."""
    if (
        not name
        or name != name.strip()
        or name in {".", ".."}
        or any(char in name for char in ("/", "\\", "\x00"))
    ):
        raise ValueError(f"invalid chore name: {name!r}")


def automatic_chore_name(suffix: str) -> str:
    """Put one allocated filename suffix in the reserved automatic namespace."""
    name = "auto_" + suffix
    if _AUTO_NAME.fullmatch(name) is None:
        raise ValueError(f"invalid automatic chore suffix: {suffix!r}")
    return name


def chore_id(name: str, *, agent_name: str) -> str:
    """Derive eight Base32 characters from the exact filename stem and agent."""
    validate_chore_name(name)
    validate_agent_name(agent_name)
    payload = int.from_bytes(sha256(name.encode("utf-8")).digest()[:5], "big")
    payload &= _KIND_BIT - 1
    if _AUTO_NAME.fullmatch(name):
        payload |= _KIND_BIT
    key = derive_id_key("toolang:chore:v1", agent_name)
    return encode_short_id(scramble_id(payload, width=8, key=key))
