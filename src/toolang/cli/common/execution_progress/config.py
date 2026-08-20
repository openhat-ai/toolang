"""Configuration owned by execution progress presentation."""

from __future__ import annotations

from collections.abc import Mapping

DEFAULT_MAX_PROGRESS_WIDTH = 120
PROGRESS_MAX_WIDTH_ENV = "TOOLANG_PROGRESS_MAX_WIDTH"


def resolve_progress_max_width(environ: Mapping[str, str]) -> int:
    """Resolve the positive maximum progress width from one environment."""

    raw = environ.get(PROGRESS_MAX_WIDTH_ENV)
    if raw is None:
        return DEFAULT_MAX_PROGRESS_WIDTH
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(
            f"{PROGRESS_MAX_WIDTH_ENV} must be a positive integer"
        ) from exc
    if value < 1:
        raise ValueError(f"{PROGRESS_MAX_WIDTH_ENV} must be a positive integer")
    return value
