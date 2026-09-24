"""Deterministic JSON encoding that preserves decimal values."""

from __future__ import annotations

from collections.abc import Mapping

import msgspec


def _encode_mapping(value: object) -> object:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"unsupported catalog JSON value: {type(value).__name__}")


_ENCODER = msgspec.json.Encoder(order="sorted", enc_hook=_encode_mapping)
_ORDERED_ENCODER = msgspec.json.Encoder(enc_hook=_encode_mapping)


def dumps(data: object, *, indent: int | None = 2, sort_keys: bool = True) -> str:
    """Serialize native JSON numbers, optionally preserving semantic key order."""

    payload = (_ENCODER if sort_keys else _ORDERED_ENCODER).encode(data)
    if indent is not None:
        payload = msgspec.json.format(payload, indent=indent)
    return payload.decode("utf-8") + "\n"
