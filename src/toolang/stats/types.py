"""Vocabulary and arithmetic shared by cached and source statistics."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Literal

Window = Literal["all", "1y", "1mo", "1w", "1d", "4h", "1h", "30m"]
WINDOWS: dict[Window, int | None] = {
    "all": None,
    "1y": 365 * 86400,
    "1mo": 30 * 86400,
    "1w": 7 * 86400,
    "1d": 86400,
    "4h": 14400,
    "1h": 3600,
    "30m": 1800,
}
PERIODS = (("all", 0), ("minute", 60), ("hour", 3600), ("day", 86400))
TERMINAL = frozenset(("succeeded", "failed", "canceled"))


@dataclass(frozen=True, slots=True)
class Scope:
    kind: Literal["agent", "thread", "root_run"] = "agent"
    id: str = ""

    def matches(self, root: str, thread: str) -> bool:
        if self.kind == "thread":
            return self.id == thread
        if self.kind == "root_run":
            return self.id == root
        return True


@dataclass(frozen=True, slots=True)
class Metrics:
    input_tokens: int | None = 0
    output_tokens: int | None = 0
    cached_tokens: int | None = 0
    spend_micros: int | None = 0
    model_calls: int = 0
    tool_calls: int = 0
    root_runs: int = 0
    spend_approximate: bool = False

    def __add__(self, other: Metrics) -> Metrics:
        def quantity(a: int | None, b: int | None) -> int | None:
            return None if a is None or b is None else a + b

        return Metrics(
            quantity(self.input_tokens, other.input_tokens),
            quantity(self.output_tokens, other.output_tokens),
            quantity(self.cached_tokens, other.cached_tokens),
            quantity(self.spend_micros, other.spend_micros),
            self.model_calls + other.model_calls,
            self.tool_calls + other.tool_calls,
            self.root_runs + other.root_runs,
            self.spend_approximate or other.spend_approximate,
        )


METRIC_FIELDS = tuple(field.name for field in fields(Metrics))


@dataclass(frozen=True, slots=True)
class Contribution:
    at: int
    root: str
    thread: str
    model: str
    metrics: Metrics


def timestamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp())


def slices(lo: int, hi: int, level: int = 0) -> list[tuple[str, int, int]]:
    """Partition an interval into disjoint complete buckets and exact edges."""
    if lo >= hi:
        return []
    levels = (("day", 86400), ("hour", 3600), ("minute", 60))
    if level == len(levels):
        return [("edge", lo, hi)]
    period, width = levels[level]
    left, right = (lo + width - 1) // width * width, hi // width * width
    if left >= right:
        return slices(lo, hi, level + 1)
    return (
        slices(lo, left, level + 1)
        + [(period, left, right)]
        + slices(right, hi, level + 1)
    )
