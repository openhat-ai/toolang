"""Environment value hashing at the persistent model catalog boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256


def environment_fingerprint(
    names: Sequence[str], environ: Mapping[str, str]
) -> tuple[tuple[str, str], ...]:
    """Hash only relevant set variables; missing and empty values stay distinct."""

    return tuple(
        (name, sha256(environ[name].encode("utf-8")).hexdigest())
        for name in sorted(set(names))
        if name in environ
    )
