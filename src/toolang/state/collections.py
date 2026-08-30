"""Public query view for resolved State capabilities."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

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


CAP_SCHEMA = CollectionSchema.from_type(
    "caps",
    CapQueryView,
    key=("kind", "name", "ref"),
    identity=IdentitySpec(
        paths=("kind", "name"),
        labels=("kind", "cap"),
        separator="/",
    ),
    exclude=("record",),
    columns=(
        ColumnSpec("KIND", ("kind",), "identity-component"),
        ColumnSpec("CAP", ("name",), "identity-component"),
        ColumnSpec("ORIGIN", ("origin",)),
        ColumnSpec("FORM", ("form",)),
        ColumnSpec("SCOPE", ("scope",)),
        ColumnSpec("SOURCE", ("source",)),
    ),
)
CAP_DEFINITION = CollectionDefinition(CAP_SCHEMA)


def cap_kind_definition(kind: EntryKind) -> CollectionDefinition[CapQueryView]:
    """Return a kind-scoped definition with local cap identities."""

    return CollectionDefinition(
        CollectionSchema.from_type(
            f"{kind}s",
            CapQueryView,
            key=("kind", "name", "ref"),
            identity=IdentitySpec(paths=("name",), labels=(kind,)),
            exclude=("record",),
            columns=(
                ColumnSpec(kind.upper(), ("name",), "identity"),
                ColumnSpec("ORIGIN", ("origin",)),
                ColumnSpec("FORM", ("form",)),
                ColumnSpec("SCOPE", ("scope",)),
                ColumnSpec("SOURCE", ("source",)),
            ),
        )
    )


def cap_dataset(
    entries: Sequence[StateCap],
    *,
    agent_name: str,
    kind: EntryKind | None = None,
) -> QueryDataset[CapQueryView]:
    """Materialize one resolved cap snapshot in combined or kind scope."""

    definition = CAP_DEFINITION if kind is None else cap_kind_definition(kind)
    return definition.dataset(
        tuple(
            _cap_view(entry, agent_name=agent_name)
            for entry in entries
            if kind is None or entry.kind == kind
        )
    )


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
    "CAP_DEFINITION",
    "CAP_SCHEMA",
    "CapQueryView",
    "cap_dataset",
    "cap_kind_definition",
]
