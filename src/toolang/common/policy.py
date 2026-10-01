"""Resource policy sentinel normalization."""

from collections.abc import Sequence
from typing import cast

from tq import Match, Query

from toolang.common.errors import ToolangError


def resolve_query_sentinels(
    values: Sequence[str], *, label: str
) -> tuple[str, ...] | None:
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{label} queries must be strings")
        value = value.strip()
        if not value:
            raise ToolangError(f"{label} queries must not be empty")
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        return ()
    if len(normalized) == 1:
        if normalized[0].lower() == "all":
            return None
        if normalized[0].lower() == "none":
            return ()
    parsed = Query.parse(normalized).validate({"key": "ref"})
    if any(
        not match.exact
        and not match.predicates
        and match.identity.lower() in {"all", "none"}
        for match in cast(tuple[Match, ...], getattr(parsed, "matches"))
    ):
        raise ToolangError(f"{label} cannot mix queries with all or none")
    return tuple(normalized)
