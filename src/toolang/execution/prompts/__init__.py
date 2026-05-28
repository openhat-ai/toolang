"""Bundled execution prompts."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """Load one bundled execution prompt."""

    return files(__package__).joinpath(name).read_text(encoding="utf-8").strip()
