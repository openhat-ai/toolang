"""Pure Base32 encoding, key derivation, and reversible permutation.

ID owners define bit layouts and versioned namespaces. This module has no
clock, allocator, persistence, or domain-specific prefixes.
"""

import hashlib
import json

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_LOOKUP = {char: index for index, char in enumerate(_ALPHABET)}
_FEISTEL_ROUNDS = 4


def derive_id_key(namespace: str, *parts: str) -> bytes:
    """Hash an unambiguously framed, case-sensitive identity into a codec key."""
    material = json.dumps(
        [namespace, *parts], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.blake2s(material).digest()


def encode_short_id(value: int, *, width: int = 8) -> str:
    """Encode an unsigned value using the lowercase Base32 alphabet."""
    _check_range(value, width)
    chars = ["0"] * width
    for index in range(width - 1, -1, -1):
        chars[index] = _ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)


def decode_short_id(value: str, *, width: int = 8) -> int:
    """Decode fixed-width Base32, accepting the local codec's normalization."""
    text = value.strip().lower()
    if len(text) != width:
        raise ValueError(f"expected {width} base32 chars, got {value!r}")
    decoded = 0
    for char in text:
        try:
            digit = _LOOKUP[char]
        except KeyError as exc:
            raise ValueError(f"unsupported id character: {char!r}") from exc
        decoded = (decoded << 5) | digit
    return decoded


def scramble_id(value: int, *, width: int, key: bytes, reverse: bool = False) -> int:
    """Permute a fixed-width code bijectively with four keyed Feistel rounds."""
    _check_range(value, width)
    bits = width * 5
    if bits % 2:
        raise ValueError("wire bits must be even")
    half = bits // 2
    mask = (1 << half) - 1
    left, right = value >> half, value & mask
    rounds = range(_FEISTEL_ROUNDS)
    for index in reversed(rounds) if reverse else rounds:
        if reverse:
            left, right = (
                right ^ _round(left, key=key, index=index, mask=mask),
                left,
            )
        else:
            left, right = (
                right,
                left ^ _round(right, key=key, index=index, mask=mask),
            )
    return (left << half) | right


def _round(value: int, *, key: bytes, index: int, mask: int) -> int:
    value_width = max(1, (mask.bit_length() + 7) // 8)
    payload = bytes((index,)) + value.to_bytes(value_width, "big")
    digest = hashlib.blake2s(payload, key=key).digest()
    return int.from_bytes(digest, "big") & mask


def _check_range(value: int, width: int) -> None:
    if width <= 0:
        raise ValueError("id width must be positive")
    if value < 0 or value >= 1 << (width * 5):
        raise ValueError(f"value out of range: {value}")
