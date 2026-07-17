"""Authored cap catalog and cap selection API."""

from __future__ import annotations

from pathlib import Path

from toolang.state import caps as cap_state
from toolang.state.prepared import EntryKind, PreparedEntry, PreparedVisibility

EntryForm = cap_state.EntryForm
EntryOrigin = cap_state.EntryOrigin
EntryScope = cap_state.EntryScope
Visibility = cap_state.Visibility

add_remote_entry = cap_state.add_remote_entry
remove_remote_entry = cap_state.remove_remote_entry
remote_entry_name = cap_state.remote_entry_name
entry_visibility = cap_state.entry_visibility
entry_origin = cap_state.entry_origin
entry_form = cap_state.entry_form
entry_scope = cap_state.entry_scope
entry_ref = cap_state.entry_ref
entry_definition_file = cap_state.entry_definition_file
entry_line = cap_state.entry_line
split_cap_selectors = cap_state.split_cap_selectors
cap_entry_matches_selector = cap_state.cap_entry_matches_selector
select_cap_entries = cap_state.select_cap_entries
list_entries = cap_state.list_entries


class CapCatalog:
    """CRUD over one shared or private authored cap location."""

    def __init__(
        self,
        root: Path,
        agent: str,
        *,
        visibility: PreparedVisibility,
    ) -> None:
        self.root = root
        self.agent = agent
        self.visibility = visibility

    def list(self, *, kinds: set[EntryKind] | None = None) -> tuple[PreparedEntry, ...]:
        return cap_state.list_local_entries(
            self.root,
            self.agent,
            visibility=self.visibility,
            kinds=kinds,
        )

    def get(self, kind: EntryKind, name: str) -> PreparedEntry | None:
        return next(
            (entry for entry in self.list(kinds={kind}) if entry.name == name),
            None,
        )

    def create(self, kind: EntryKind, name: str, text: str) -> Path:
        if self.get(kind, name) is not None:
            raise FileExistsError(f"local {kind} already exists: {name}")
        return cap_state.write_local_entry(
            self.root,
            self.agent,
            visibility=self.visibility,
            kind=kind,
            name=name,
            text=text,
        )

    def update(self, kind: EntryKind, name: str, text: str) -> Path:
        if self.get(kind, name) is None:
            raise FileNotFoundError(f"local {kind} not found: {name}")
        return cap_state.write_local_entry(
            self.root,
            self.agent,
            visibility=self.visibility,
            kind=kind,
            name=name,
            text=text,
        )

    def read(self, kind: EntryKind, name: str) -> str:
        return cap_state.read_local_entry(
            self.root,
            self.agent,
            visibility=self.visibility,
            kind=kind,
            name=name,
        )

    def remove(self, kind: EntryKind, name: str) -> bool:
        return cap_state.remove_local_entry(
            self.root,
            self.agent,
            visibility=self.visibility,
            kind=kind,
            name=name,
        )

    def snapshot(self) -> tuple[PreparedEntry, ...]:
        return self.list()
