"""Capability ref resolution and local-entry loading helpers."""

from __future__ import annotations

from pathlib import Path
from typing import cast, get_args

from toolang.concepts.caps import CapKind
from toolang.concepts.layout import AgentHome, ToolangRoot
from toolang.concepts.persisted.sync_state import LockEntry, LockedAgentRefs
from toolang.errors import ExternalDependencyUnavailableError
from toolang.errors import ToolangError
from toolang.program.ast import UseDecl

from . import github

ALL_CAP_KINDS = get_args(CapKind)


def resolve_cap_uses(
    uses: list[UseDecl],
    *,
    scope_label: str,
    cached_refs: LockedAgentRefs | None = None,
    skip_unavailable: bool = False,
) -> LockedAgentRefs:
    """Resolve authored capability imports to locked ref entries."""

    refs = LockedAgentRefs()
    cached = cached_refs or LockedAgentRefs()
    for use in uses:
        if use.kind not in ALL_CAP_KINDS:
            raise ToolangError(f"Unsupported cap kind in {scope_label}: {use.kind}")
        kind = cast(CapKind, use.kind)
        entries = refs.entries(kind)
        cached_entry = _cached_ref_entry(cached.entries(kind), use.reference)
        cap_name = _cap_name_from_ref(use.reference)
        existing = entries.get(cap_name)
        if existing is not None and existing.ref != use.reference:
            raise ToolangError(
                f"Conflicting {use.kind} refs resolve to the same name in {scope_label}: {cap_name}"
            )
        if cached_entry is not None:
            entry = cached_entry
            resolved_name = cap_name
        else:
            try:
                resolved = github.resolve_github_cap_ref(kind, use.reference)
            except ExternalDependencyUnavailableError:
                if skip_unavailable:
                    continue
                raise
            entry = LockEntry(
                ref=resolved.ref,
                repo=resolved.repo,
                path=resolved.path,
                rev=resolved.rev,
            )
            resolved_name = resolved.name
        if existing is not None and existing != entry:
            raise ToolangError(
                f"Conflicting {use.kind} refs resolve to the same name in {scope_label}: {resolved_name}"
            )
        entries[resolved_name] = entry
    return refs.sorted_copy()


def _cached_ref_entry(
    entries: dict[str, LockEntry],
    reference: str,
) -> LockEntry | None:
    name = _cap_name_from_ref(reference)
    entry = entries.get(name)
    if entry is None or entry.ref != reference:
        return None
    return entry


def _cap_name_from_ref(ref: str) -> str:
    return ref.rpartition("/")[2] or ref


def load_local_entries_for_scope(
    *,
    root: Path,
    scope_root: Path,
    scope: str,
) -> LockedAgentRefs:
    refs = LockedAgentRefs()
    root_layout = ToolangRoot.resolve(root)
    for kind in ALL_CAP_KINDS:
        kind_dir = (
            AgentHome.resolve(root).shared_caps_dir(kind)
            if scope == "shared"
            else root_layout.global_caps_dir(kind)
        )
        entries = refs.entries(kind)
        if not kind_dir.exists():
            continue
        if kind == "skill":
            for item in sorted(kind_dir.iterdir()):
                if not item.is_dir() or not (item / "SKILL.md").exists():
                    continue
                entries[item.name] = LockEntry(path=str(item.relative_to(scope_root)))
            continue
        for item in sorted(kind_dir.glob("*.md")):
            entries[item.stem] = LockEntry(path=str(item.relative_to(scope_root)))
    return refs.sorted_copy()
