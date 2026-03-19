from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):  # pragma: no branch
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

import tomli_w


def load_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def write_toml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(tomli_w.dumps(data), encoding="utf-8")
