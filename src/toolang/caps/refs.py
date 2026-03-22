"""Capability ref resolution and local-entry loading helpers."""

from __future__ import annotations

from pathlib import Path
from typing import cast, get_args

from toolang.concepts.caps import CapKind
from toolang.concepts.persisted.sync_state import LockEntry, LockedAgentRefs
from toolang.errors import ToolangError
from toolang.program.ast import UseDecl

from . import github

ALL_CAP_KINDS = get_args(CapKind)


def resolve_cap_uses(uses: list[UseDecl], *, scope_label: str) -> LockedAgentRefs:
    """Resolve authored capability imports to locked ref entries."""

    refs = LockedAgentRefs()
    for use in uses:
        if use.kind not in ALL_CAP_KINDS:
            raise ToolangError(f"Unsupported cap kind in {scope_label}: {use.kind}")
        kind = cast(CapKind, use.kind)
        resolved = github.resolve_github_cap_ref(kind, use.reference)
        entries = refs.entries(kind)
        entry = LockEntry(
            ref=resolved.ref,
            repo=resolved.repo,
            path=resolved.path,
            rev=resolved.rev,
        )
        existing = entries.get(resolved.name)
        if existing is not None and existing != entry:
            raise ToolangError(
                f"Conflicting {use.kind} refs resolve to the same name in {scope_label}: {resolved.name}"
            )
        entries[resolved.name] = entry
    return refs.sorted_copy()


def load_local_entries_for_scope(
    *,
    root: Path,
    scope_root: Path,
    scope: str,
) -> LockedAgentRefs:
    from toolang.layout import global_caps_dir, shared_caps_dir

    refs = LockedAgentRefs()
    for kind in ALL_CAP_KINDS:
        kind_dir = (
            shared_caps_dir(root, kind) if scope == "shared" else global_caps_dir(root, kind)
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
