"""State-owned configuration snapshot parsing."""

from __future__ import annotations

import tomllib
from typing import cast


def parse_config(content: bytes) -> dict[str, object]:
    """Parse one UTF-8 TOML configuration snapshot."""

    return cast(dict[str, object], tomllib.loads(content.decode("utf-8")))
