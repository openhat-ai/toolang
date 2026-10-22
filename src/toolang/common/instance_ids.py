"""Pure, ordered identities for Toolang areas and nonresident roots."""

from typing import Literal

from .id_codec import decode_short_id, derive_id_key, encode_short_id

InstanceMarker = Literal["h", "s"]


def _field(value: int, bits: int, width: int) -> str:
    if type(value) is not int or not 0 <= value < 1 << bits:
        raise ValueError(
            f"instance ID component must be an unsigned {bits}-bit integer"
        )
    return encode_short_id(value, width=width)


def _decode(value: str, bits: int, width: int) -> int:
    result = decode_short_id(value, width=width)
    if _field(result, bits, width) != value:
        raise ValueError("instance ID must use canonical lowercase base32")
    return result


def encode_instance_id(
    marker: InstanceMarker, source: int, timestamp_ms: int | None = None
) -> str:
    """Encode each bounded field independently, preserving creation order."""
    if marker == "h" and timestamp_ms is None:
        return "h" + _field(source, 48, 10)
    if marker == "s" and timestamp_ms is not None:
        return "s" + _field(source, 32, 7) + _field(timestamp_ms, 48, 10)
    raise ValueError("instance ID requires h with a source hash or s with a timestamp")


def decode_instance_id(
    value: str, *, marker: InstanceMarker | None = None
) -> tuple[int, int | None]:
    """Require a complete canonical h/s ID; old spellings are unsupported."""
    if not isinstance(value, str) or not value or (marker and value[0] != marker):
        raise ValueError("instance ID requires a complete h/s ID")
    if value[0] == "h" and len(value) == 11:
        return _decode(value[1:], 48, 10), None
    if value[0] == "s" and len(value) == 18:
        return _decode(value[1:8], 32, 7), _decode(value[8:], 48, 10)
    raise ValueError(
        "instance ID requires h plus 10 or s plus 17 lowercase base32 characters"
    )


def _tag(namespace: str, source: str, bits: int) -> int:
    return int.from_bytes(derive_id_key(namespace, source), "big") >> (256 - bits)


def area_id(toolang_root: str) -> str:
    return _field(_tag("area:v1", toolang_root, 32), 32, 7)


def visiting_instance_id(source_ref: str) -> str:
    return encode_instance_id("h", _tag("hotel:v1", source_ref, 48))


def roaming_instance_id(source_path: str, timestamp_ms: int) -> str:
    return encode_instance_id("s", _tag("site:v1", source_path, 32), timestamp_ms)


def roaming_timestamp_ms(instance_id: str) -> int:
    timestamp = decode_instance_id(instance_id, marker="s")[1]
    assert timestamp is not None
    return timestamp
