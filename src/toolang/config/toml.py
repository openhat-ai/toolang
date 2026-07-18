"""TOML configuration file loading."""

from __future__ import annotations

from pathlib import Path
import tomllib
from typing import cast


def load_optional_toml(path: Path) -> dict[str, object]:
    """Load a TOML object, returning an empty mapping when the file is absent."""

    if not path.is_file():
        return {}
    return cast(dict[str, object], tomllib.loads(path.read_text(encoding="utf-8")))
