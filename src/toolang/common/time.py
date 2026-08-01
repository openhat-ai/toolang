"""Package-neutral time helpers."""

from __future__ import annotations

import time


def utc_now() -> str:
    """Return the current UTC time in Toolang's durable text format."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def elapsed_ms(started_at: float) -> int:
    """Return elapsed monotonic time in non-negative milliseconds."""

    return max(0, round((time.perf_counter() - started_at) * 1000))
