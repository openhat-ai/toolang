"""Pure, canonical 32/48-bit instance identities."""

from typing import Literal

from .id_codec import decode_short_id, derive_id_key, encode_short_id

InstanceMarker = Literal["v", "r"]
_WIDTH = 16
_SUFFIX_BITS = 48


def encode_instance_id(marker: InstanceMarker, prefix: int, suffix: int) -> str:
    """Pack a placement marker and independently bounded unsigned fields."""
    if marker not in {"v", "r"}:
        raise ValueError("instance ID requires a v/r placement marker")
    for value, bits in ((prefix, 32), (suffix, _SUFFIX_BITS)):
        if type(value) is not int or not 0 <= value < 1 << bits:
            raise ValueError(
                f"instance ID component must be an unsigned {bits}-bit integer"
            )
    return marker + encode_short_id((prefix << _SUFFIX_BITS) | suffix, width=_WIDTH)


def decode_instance_id(
    value: str, *, marker: InstanceMarker | None = None
) -> tuple[int, int]:
    """Require an exact canonical ID, including its placement marker."""
    if (
        not isinstance(value, str)
        or len(value) != _WIDTH + 1
        or value[0] not in {"v", "r"}
        or (marker is not None and value[0] != marker)
    ):
        raise ValueError(
            "instance ID requires v/r followed by 16 lowercase base32 characters"
        )
    packed = decode_short_id(value[1:], width=_WIDTH)
    if encode_short_id(packed, width=_WIDTH) != value[1:]:
        raise ValueError("instance ID must use canonical lowercase base32")
    return packed >> _SUFFIX_BITS, packed & ((1 << _SUFFIX_BITS) - 1)


def _tag(namespace: str, source: str, bits: int) -> int:
    return int.from_bytes(derive_id_key(namespace, source), "big") >> (256 - bits)


def visiting_instance_id(toolang_root: str, source_ref: str) -> str:
    """Identify a canonical requested URL within its resolved Toolang root."""
    return encode_instance_id(
        "v",
        _tag("instance-root:v1", toolang_root, 32),
        _tag("visiting-url:v1", source_ref, 48),
    )


def roaming_instance_id(source_path: str, timestamp_ms: int) -> str:
    """Identify one logical launch of a resolved canonical source path."""
    return encode_instance_id(
        "r", _tag("roaming-path:v1", source_path, 32), timestamp_ms
    )


def roaming_timestamp_ms(instance_id: str) -> int:
    """Return a roaming launch's logical timestamp, also across path prefixes."""
    return decode_instance_id(instance_id, marker="r")[1]
