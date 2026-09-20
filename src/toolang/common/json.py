"""Deterministic JSON encoding that preserves decimal values."""

from __future__ import annotations

from collections.abc import Mapping

import msgspec


def _encode_mapping(value: object) -> object:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"unsupported catalog JSON value: {type(value).__name__}")


_ENCODER = msgspec.json.Encoder(order="sorted", enc_hook=_encode_mapping)


def dumps(data: object, *, indent: int | None = 2) -> str:
    """Serialize catalog data deterministically using native JSON numbers."""

    payload = _ENCODER.encode(data)
    if indent is not None:
        payload = msgspec.json.format(payload, indent=indent)
    return payload.decode("utf-8") + "\n"
