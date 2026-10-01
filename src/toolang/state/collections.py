"""Transient cap selection and public records."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

from tq import Query

from toolang.common.errors import ToolangError
from toolang.common.types import SetOperator

from .state import (
    StateCap,
    entry_form,
    entry_origin,
    entry_ref,
    entry_scope,
    entry_source,
)
from .types import EntryKind


@dataclass(frozen=True, slots=True)
class CapView:
    record: StateCap
    data: dict[str, object]
    source_ref: str

    @property
    def kind(self) -> EntryKind:
        return self.record.kind

    @property
    def name(self) -> str:
        return self.record.name

    @property
    def ref(self) -> str:
        return str(self.data["ref"])


def cap_view(
    entry: StateCap, *, root: Path | None, agent_name: str, allowed: bool = True
) -> CapView:
    description = entry.meta.get("description")
    return CapView(
        entry,
        {
            "ref": f"{entry.kind}/{entry.name}",
            "name": entry.name,
            "description": description if isinstance(description, str) else None,
            "location": entry_source(entry, agent_name=agent_name, root=root),
            "tags": [
                "ready" if allowed else "not_allowed",
                entry_origin(entry),
                entry_scope(entry, agent_name=agent_name),
                entry_form(entry),
            ],
        },
        source_ref=entry_ref(entry, agent_name=agent_name),
    )


@dataclass(frozen=True, slots=True)
class CapCollection:
    items: tuple[CapView, ...]

    def query(self, queries: str | Sequence[str] | None = None) -> tuple[CapView, ...]:
        if queries is None:
            return self.items
        query = Query.parse(queries).validate({"key": "ref"})
        return tuple(view for view in self.items if query.match(view.data) is not None)

    def require_each(self, queries: Sequence[str], *, label: str) -> None:
        missing = [value for value in queries if not self.query(value)]
        if missing:
            raise ToolangError(f"{label} query matched no items: {', '.join(missing)}")

    def apply(
        self, operations: Sequence[tuple[SetOperator, Sequence[str]]]
    ) -> tuple[CapView, ...]:
        def key(view: CapView) -> tuple[str, str]:
            return view.ref, view.source_ref

        active = {key(view) for view in self.items}
        for operator, queries in operations:
            matches = {key(view) for view in self.query(queries)}
            if operator == "=":
                active.intersection_update(matches)
            elif operator == "+=":
                active.update(matches)
            elif operator == "-=":
                active.difference_update(matches)
            else:
                raise ToolangError(f"unknown collection set operator: {operator!r}")
        return tuple(view for view in self.items if key(view) in active)


def cap_collection(
    entries: Sequence[StateCap],
    *,
    root: Path | None,
    agent_name: str,
    kind: EntryKind | None = None,
    allowed: Collection[tuple[EntryKind, str]] | None = None,
) -> CapCollection:
    views: dict[tuple[str, str], CapView] = {}
    for entry in entries:
        if kind is None or entry.kind == kind:
            view = cap_view(
                entry,
                agent_name=agent_name,
                root=root,
                allowed=allowed is None or (entry.kind, entry.name) in allowed,
            )
            views.setdefault((view.ref, view.source_ref), view)
    return CapCollection(tuple(views.values()))


def query_cap_views(
    entries: Sequence[StateCap],
    *,
    root: Path | None,
    agent_name: str,
    queries: Sequence[str] | None,
    allowed: Collection[tuple[EntryKind, str]] | None = None,
) -> tuple[CapView, ...]:
    return cap_collection(
        entries, root=root, agent_name=agent_name, allowed=allowed
    ).query(queries)
