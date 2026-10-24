"""Pure, ordered short codes for roots and nonresident places."""

from typing import Literal

from .id_codec import decode_short_id, derive_id_key, encode_short_id
from .ids import ID_EPOCH

NonresidentPlacement = Literal["visiting", "roaming"]
PLACE_CODE_EPOCH_MS = int(ID_EPOCH.timestamp()) * 1_000
_HASH_BITS = 48
_TIME_BITS = 45
_VISITING_TAG = 0b11


def _check_component(value: int, bits: int) -> None:
    if type(value) is not int or not 0 <= value < 1 << bits:
        raise ValueError(f"place code component must be an unsigned {bits}-bit integer")


def _field(value: int, bits: int, width: int) -> str:
    _check_component(value, bits)
    return encode_short_id(value, width=width)


def _decode(value: str, bits: int, width: int) -> int:
    result = decode_short_id(value, width=width)
    if _field(result, bits, width) != value:
        raise ValueError("place code must use canonical lowercase base32")
    return result


def encode_place_code(
    placement: NonresidentPlacement, source: int, timestamp_ms: int | None = None
) -> str:
    """Pack the type, source hash, and optional epoch-relative milliseconds."""
    _check_component(source, _HASH_BITS)
    if placement == "visiting" and timestamp_ms is None:
        return encode_short_id((_VISITING_TAG << _HASH_BITS) | source, width=10)
    if placement == "roaming" and timestamp_ms is not None:
        if type(timestamp_ms) is not int:
            raise ValueError("place code timestamp component must be an integer")
        elapsed_ms = timestamp_ms - PLACE_CODE_EPOCH_MS
        _check_component(elapsed_ms, _TIME_BITS)
        return encode_short_id((source << _TIME_BITS) | elapsed_ms, width=19)
    raise ValueError(
        "place code requires visiting with a source hash or roaming with a timestamp"
    )


def decode_place_code(
    value: str, *, placement: NonresidentPlacement | None = None
) -> tuple[int, int | None]:
    """Require the complete fixed width, canonical spelling, and two-bit type."""
    if not isinstance(value, str):
        raise ValueError("place code must be a string")
    if len(value) == 10 and placement in {None, "visiting"}:
        packed = _decode(value, 50, 10)
        if packed >> _HASH_BITS == _VISITING_TAG:
            return packed & ((1 << _HASH_BITS) - 1), None
    if len(value) == 19 and placement in {None, "roaming"}:
        packed = _decode(value, 95, 19)
        if packed >> (_HASH_BITS + _TIME_BITS) == 0b00:
            return packed >> _TIME_BITS, (
                packed & ((1 << _TIME_BITS) - 1)
            ) + PLACE_CODE_EPOCH_MS
    raise ValueError(
        "place code requires 10 lowercase base32 characters with type 11 "
        "or 19 with type 00"
    )


def _tag(namespace: str, source: str, bits: int) -> int:
    return int.from_bytes(derive_id_key(namespace, source), "big") >> (256 - bits)


def root_code(root: str) -> str:
    return _field(_tag("place:v1", root, 32), 32, 7)


def visiting_place_code(source_ref: str) -> str:
    return encode_place_code("visiting", _tag("visiting:v1", source_ref, 48))


def roaming_place_code(source_path: str, timestamp_ms: int) -> str:
    return encode_place_code(
        "roaming", _tag("roaming:v1", source_path, 48), timestamp_ms
    )


def place_source_id(placement: NonresidentPlacement, source: str) -> str:
    """Return the raw source hash; roaming codes start with this stable prefix."""
    if placement not in {"visiting", "roaming"}:
        raise ValueError("invalid nonresident placement")
    namespace = f"{placement}:v1"
    return _field(_tag(namespace, source, _HASH_BITS), _HASH_BITS, 10)


def roaming_timestamp_ms(place_code: str) -> int:
    timestamp = decode_place_code(place_code, placement="roaming")[1]
    assert timestamp is not None
    return timestamp
