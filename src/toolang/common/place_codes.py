"""Pure, ordered short codes for roots and nonresident places."""

from typing import Literal

from .id_codec import decode_short_id, derive_id_key, encode_short_id

PlaceMarker = Literal["t", "s"]


def _field(value: int, bits: int, width: int) -> str:
    if type(value) is not int or not 0 <= value < 1 << bits:
        raise ValueError(f"place code component must be an unsigned {bits}-bit integer")
    return encode_short_id(value, width=width)


def _decode(value: str, bits: int, width: int) -> int:
    result = decode_short_id(value, width=width)
    if _field(result, bits, width) != value:
        raise ValueError("place code must use canonical lowercase base32")
    return result


def encode_place_code(
    marker: PlaceMarker, source: int, timestamp_ms: int | None = None
) -> str:
    """Encode each bounded field independently, preserving creation order."""
    if marker == "t" and timestamp_ms is None:
        return "t" + _field(source, 48, 10)
    if marker == "s" and timestamp_ms is not None:
        return "s" + _field(source, 32, 7) + _field(timestamp_ms, 48, 10)
    raise ValueError("place code requires t with a source hash or s with a timestamp")


def decode_place_code(
    value: str, *, marker: PlaceMarker | None = None
) -> tuple[int, int | None]:
    """Require a complete canonical t/s place code; old spellings are unsupported."""
    if not isinstance(value, str) or not value or (marker and value[0] != marker):
        raise ValueError("place code requires a complete t/s place code")
    if value[0] == "t" and len(value) == 11:
        return _decode(value[1:], 48, 10), None
    if value[0] == "s" and len(value) == 18:
        return _decode(value[1:8], 32, 7), _decode(value[8:], 48, 10)
    raise ValueError(
        "place code requires t plus 10 or s plus 17 lowercase base32 characters"
    )


def _tag(namespace: str, source: str, bits: int) -> int:
    return int.from_bytes(derive_id_key(namespace, source), "big") >> (256 - bits)


def root_code(root: str) -> str:
    return _field(_tag("place:v1", root, 32), 32, 7)


def visiting_place_code(source_ref: str) -> str:
    return encode_place_code("t", _tag("visiting:v1", source_ref, 48))


def roaming_place_code(source_path: str, timestamp_ms: int) -> str:
    return encode_place_code("s", _tag("roaming:v1", source_path, 32), timestamp_ms)


def roaming_timestamp_ms(place_code: str) -> int:
    timestamp = decode_place_code(place_code, marker="s")[1]
    assert timestamp is not None
    return timestamp
