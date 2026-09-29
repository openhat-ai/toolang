"""Package-neutral time helpers."""

from __future__ import annotations

import time


def utc_now() -> str:
    """Return the current UTC time in Toolang's durable text format."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def elapsed_ms(started_at: float) -> int:
    """Return elapsed monotonic time in non-negative milliseconds."""

    return max(0, round((time.perf_counter() - started_at) * 1000))


def format_duration(seconds: float) -> str:
    """Render milliseconds below one second, then whole units without padding."""

    seconds = max(0.0, seconds)
    if seconds == 0:
        return "0s"
    if seconds < 1:
        milliseconds = round(seconds * 1000)
        if milliseconds < 1000:
            return f"{milliseconds}ms"
    hours, remainder = divmod(round(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes}m{seconds}s"
    if minutes:
        return f"{minutes}m{seconds}s"
    return f"{seconds}s"
