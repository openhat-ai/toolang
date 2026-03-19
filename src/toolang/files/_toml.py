from __future__ import annotations

from pathlib import Path
from typing import Any

try:  # pragma: no cover - Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

import tomli_w


def load_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def write_toml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(tomli_w.dumps(data), encoding="utf-8")
