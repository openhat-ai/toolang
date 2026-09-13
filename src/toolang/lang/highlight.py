"""Grammar-owned highlight captures and deterministic byte spans."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from heapq import heappop, heappush

from tree_sitter import Query, QueryCursor
from tree_sitter_toolang import HIGHLIGHTS_QUERY

from .cst import language, parse


@dataclass(frozen=True, slots=True)
class Capture:
    start_byte: int
    end_byte: int
    name: str
    pattern: int = 0


@lru_cache(maxsize=1)
def _query() -> Query:
    return Query(language(), HIGHLIGHTS_QUERY)


def captures(source: bytes) -> list[Capture]:
    tree = parse(source)
    return [
        Capture(node.start_byte, node.end_byte, name, pattern)
        for pattern, matches in QueryCursor(_query()).matches(tree.root_node)
        for name, nodes in matches.items()
        for node in nodes
        if node.end_byte > node.start_byte
    ]


def resolve(captures: Iterable[Capture]) -> list[Capture]:
    """Resolve overlaps in O(n log n), preferring specific, later captures."""
    ordered = sorted(
        (item for item in captures if item.end_byte > item.start_byte),
        key=lambda item: item.start_byte,
    )
    boundaries = sorted(
        {point for item in ordered for point in (item.start_byte, item.end_byte)}
    )
    active: list[tuple[int, int, str, int, Capture]] = []
    result: list[Capture] = []
    index = 0
    for start, end in zip(boundaries, boundaries[1:]):
        while index < len(ordered) and ordered[index].start_byte <= start:
            item = ordered[index]
            heappush(
                active,
                (
                    item.end_byte - item.start_byte,
                    -item.pattern,
                    item.name,
                    index,
                    item,
                ),
            )
            index += 1
        while active and active[0][-1].end_byte <= start:
            heappop(active)
        if active:
            item = active[0][-1]
            if result and result[-1].end_byte == start and result[-1].name == item.name:
                previous = result[-1]
                result[-1] = Capture(previous.start_byte, end, item.name, item.pattern)
            else:
                result.append(Capture(start, end, item.name, item.pattern))
    return result
