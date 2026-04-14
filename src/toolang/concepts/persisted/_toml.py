"""Small TOML read/write helpers for persisted config documents."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomllib
import tomli_w


def load_toml(path: Path) -> dict[str, Any]:
    """Load one TOML document from disk."""

    return tomllib.loads(path.read_text(encoding="utf-8"))


def write_toml(path: Path, data: dict[str, Any]) -> None:
    """Write one TOML document to disk."""

    path.write_text(tomli_w.dumps(data), encoding="utf-8")
