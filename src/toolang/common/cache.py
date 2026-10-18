"""Canonical content fingerprints for captured setup inputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path

from toolang.common.json import dumps


def digest(value: object) -> str:
    """Return a portable content digest for any canonical value."""

    return text_digest(dumps(canonical_value(value), indent=None))


def text_digest(value: str) -> str:
    """Return a ``sha256:`` digest for one text payload."""

    return f"sha256:{sha256(value.encode('utf-8')).hexdigest()}"


def canonical_value(value: object) -> object:
    """Return a deterministic, JSON-safe projection of a cache identity value."""

    if isinstance(value, Mapping):
        return {
            str(key): canonical_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [canonical_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return repr(value)
