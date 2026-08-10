"""Bundled executor prompts."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """Load one bundled execution prompt."""

    return (
        files("toolang.execution.executor.prompts")
        .joinpath(name)
        .read_text(encoding="utf-8")
        .strip()
    )
