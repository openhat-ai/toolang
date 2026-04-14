"""Environment-loading helpers."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values


def load_runtime_environ(
    toolang_root: Path,
    agent_name: str,
    *,
    base_environ: Mapping[str, str],
) -> dict[str, str]:
    """Load runtime environment with root and agent .env defaults."""

    merged = _load_dotenv(toolang_root / ".env")
    merged.update(_load_dotenv(toolang_root / "agents" / agent_name / ".env"))
    merged.update(dict(base_environ))
    return merged


def _load_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values = dotenv_values(path)
    return {
        key: value
        for key, value in values.items()
        if isinstance(key, str) and isinstance(value, str)
    }
