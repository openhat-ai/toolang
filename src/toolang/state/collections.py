"""Public query view for resolved State capabilities."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import groupby

from toolang.common.query import (
    CollectionDefinition,
    CollectionSchema,
    ColumnSpec,
    IdentitySpec,
    QueryDataset,
)

from .state import (
    StateCap,
    entry_definition_file,
    entry_form,
    entry_origin,
    entry_ref,
    entry_scope,
    entry_source,
)
from .types import CapForm, CapScope, EntryKind, EntryShape, SourceOrigin


@dataclass(frozen=True, slots=True)
class CapQueryView:
    """Explicitly public resolved-cap query representation."""

    record: object
    kind: EntryKind
    name: str
    ref: str
    description: str | None
    scope: CapScope
    origin: SourceOrigin
    form: CapForm
    source: str
    definition: str
    shape: EntryShape
    editable: bool
    line: int | None


_COLLECTION_BY_KIND: dict[EntryKind, str] = {
    "psyche": "psyches",
    "skill": "skills",
    "service": "services",
    "prompt": "prompts",
}
_CAP_COLUMNS = (
    ColumnSpec("CAP", ("name",), "identity"),
    ColumnSpec("DESCRIPTION", ("description",), "truncate"),
    ColumnSpec("SCOPE", ("scope",)),
    ColumnSpec("FORM", ("form",)),
    ColumnSpec("SOURCE", ("source",)),
)


def cap_kind_definition(kind: EntryKind) -> CollectionDefinition[CapQueryView]:
    """Return one concrete cap-kind collection definition."""

    return CollectionDefinition(
        CollectionSchema.from_type(
            f"{kind}s",
            CapQueryView,
            key=("kind", "name", "ref"),
            identity=IdentitySpec(
                paths=("name",),
                labels=(kind, kind),
                separator="/",
                bound=(kind,),
            ),
            exclude=("record", "kind"),
            columns=_CAP_COLUMNS,
        )
    )


def cap_dataset(
    entries: Sequence[StateCap],
    *,
    agent_name: str,
    kind: EntryKind,
) -> QueryDataset[CapQueryView]:
    """Materialize one resolved base-cap collection."""

    definition = cap_kind_definition(kind)
    return definition.dataset(
        tuple(
            _cap_view(entry, agent_name=agent_name)
            for entry in entries
            if entry.kind == kind
        )
    )


def query_cap_views(
    entries: Sequence[StateCap],
    *,
    agent_name: str,
    queries: Sequence[str] | None,
) -> tuple[CapQueryView, ...]:
    """Query the stable union of the four concrete cap collections."""

    views = tuple(_cap_view(entry, agent_name=agent_name) for entry in entries)
    selected_keys: set[tuple[EntryKind, str, str]] = set()
    for kind in _COLLECTION_BY_KIND:
        dataset = cap_kind_definition(kind).dataset(
            tuple(view for view in views if view.kind == kind)
        )
        selected_keys.update(
            (view.kind, view.name, view.ref) for view in dataset.query(queries)
        )
    return tuple(
        view for view in views if (view.kind, view.name, view.ref) in selected_keys
    )


def cap_table(
    views: Sequence[CapQueryView],
    *,
    kind: EntryKind | None = None,
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Render cap identities and metadata in the supplied order."""

    if kind is not None:
        dataset = cap_kind_definition(kind).dataset(tuple(views))
        return dataset.table()
    rows: list[tuple[str, ...]] = []
    for cap_kind, group in groupby(views, key=lambda view: view.kind):
        dataset = cap_kind_definition(cap_kind).dataset(tuple(group))
        rows.extend(dataset.table()[1])
    return tuple(column.label for column in _CAP_COLUMNS), tuple(rows)


def _cap_view(entry: StateCap, *, agent_name: str) -> CapQueryView:
    description = entry.meta.get("description")
    return CapQueryView(
        record=entry,
        kind=entry.kind,
        name=entry.name,
        ref=entry_ref(entry, agent_name=agent_name),
        description=description if isinstance(description, str) else None,
        scope=entry_scope(entry, agent_name=agent_name),
        origin=entry_origin(entry),
        form=entry_form(entry),
        source=entry_source(entry, agent_name=agent_name),
        definition=entry_definition_file(entry),
        shape=entry.shape,
        editable=entry_origin(entry) == "local" and entry_form(entry) == "authored",
        line=entry.source.line,
    )


__all__ = [
    "CapQueryView",
    "cap_table",
    "cap_dataset",
    "cap_kind_definition",
    "query_cap_views",
]
