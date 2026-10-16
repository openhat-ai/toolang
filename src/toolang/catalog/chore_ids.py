"""Uniform agent-bound chore IDs from exact authored names."""

from hashlib import sha256

from toolang.common.id_codec import derive_id_key, encode_short_id, scramble_id
from toolang.common.layout import validate_agent_name


def validate_chore_name(name: str) -> None:
    """Validate a portable file/declaration name without binding an agent."""
    if (
        not name
        or name != name.strip()
        or name in {".", ".."}
        or any(char in name for char in ("/", "\\", "\x00"))
    ):
        raise ValueError(f"invalid chore name: {name!r}")


def chore_id(name: str, *, agent_name: str) -> str:
    """Derive eight Base32 characters uniformly, regardless of name format."""
    validate_chore_name(name)
    validate_agent_name(agent_name)
    payload = int.from_bytes(sha256(name.encode("utf-8")).digest()[:5], "big")
    key = derive_id_key("toolang:chore:v1", agent_name)
    return encode_short_id(scramble_id(payload, width=8, key=key))
