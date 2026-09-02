"""Shared human formatting for execution facts."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from ..output import parse_utc_timestamp
from .formatting import count


def execution_count_fact(runs: int, model_calls: int, tool_calls: int) -> str:
    """Return one compact fact for execution activity counts."""

    return " ".join(
        fact
        for fact in (
            count(runs, "run") if runs else "",
            count(model_calls, "model") if model_calls else "",
            count(tool_calls, "tool") if tool_calls else "",
        )
        if fact
    )


def elapsed_fact(started_at: str, finished_at: str) -> str:
    """Return a compact human duration without changing timestamp precision."""

    if not started_at or not finished_at:
        return ""
    try:
        started = parse_utc_timestamp(started_at)
        finished = parse_utc_timestamp(finished_at)
        if started is None or finished is None:
            return ""
        duration = max(0.0, (finished - started).total_seconds())
    except (TypeError, ValueError):
        return ""
    if duration < 1:
        milliseconds = round(duration * 1000)
        if milliseconds < 1000:
            return f"{milliseconds}ms"
    rounded = round(duration)
    if rounded < 60:
        return f"{rounded}s"
    minutes, seconds = divmod(rounded, 60)
    return f"{minutes}m" if seconds == 0 else f"{minutes}m{seconds:02d}s"


def compact_count(value: int) -> str:
    """Return one compact integer count for a facts bar."""

    for threshold, suffix in ((1_000_000, "m"), (1_000, "k")):
        if value >= threshold:
            rendered = value / threshold
            return f"{rendered:.1f}".rstrip("0").rstrip(".") + suffix
    return str(value)


def token_fact(
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    reasoning_complete: bool = True,
) -> str:
    """Return compact inclusive input and output token usage."""

    cache = ""
    if cache_read_tokens is not None and input_tokens > 0:
        ratio = cache_read_tokens / input_tokens * 100
        cache = f"({ratio:.1f}%)"
    reasoning = (
        f"({compact_count(reasoning_tokens)}{'' if reasoning_complete else '+'})"
        if reasoning_tokens is not None
        else ""
    )
    return (
        f"↑{compact_count(input_tokens)}{cache} "
        f"↓{compact_count(output_tokens)}{reasoning}"
    )


def cost_fact(amount: Decimal, *, approximate: bool) -> str:
    """Return one compact USD cost with adaptive nonzero precision."""

    if amount == 0:
        return ""
    if amount < 0:
        raise ValueError("model cost must be non-negative")
    prefix = "≈$" if approximate else "$"
    for places in (2, 4):
        quantum = Decimal(1).scaleb(-places)
        rounded = amount.quantize(quantum, rounding=ROUND_HALF_UP)
        if rounded:
            rendered = f"{rounded:f}".rstrip("0").rstrip(".")
            return f"{prefix}{rendered}"
    return "≲$0.0001" if approximate else "<$0.0001"
