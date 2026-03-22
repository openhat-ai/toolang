from __future__ import annotations

import shutil
from pathlib import Path

from toolang_caps.files import (
    inline_cap_meta_path,
    inline_cap_path,
    remove_stale_text_cap_materializations,
    sync_file_cap_materialization,
    sync_text_cap_materialization,
)
from toolang_concepts.caps import (
    CapContent,
    CapRef,
    CapSidecar,
    InlineCapKind,
    section_name,
)
from toolang_concepts.persisted.sync_state import LockEntry

from .. import remote


def sync_scope_text_caps(
    sync_root: Path,
    kind: InlineCapKind,
    entries: dict[str, LockEntry],
    *,
    scope_source_root: Path,
) -> None:
    expected_names = _sync_locked_text_caps(
        sync_root,
        kind,
        entries,
        scope_source_root=scope_source_root,
    )
    remove_stale_text_cap_materializations(sync_root, kind, expected_names)


def sync_agent_text_caps(
    sync_root: Path,
    kind: InlineCapKind,
    entries: dict[str, LockEntry],
    inline_caps: list[CapContent],
    *,
    scope_source_root: Path,
) -> None:
    expected_names = _sync_locked_text_caps(
        sync_root,
        kind,
        entries,
        scope_source_root=scope_source_root,
    )
    for cap in inline_caps:
        sync_text_cap_materialization(
            sync_root,
            cap.kind,
            cap.name,
            cap.raw_text,
            language=cap.language,
            params=cap.params,
        )
        expected_names.add(cap.name)
    remove_stale_text_cap_materializations(sync_root, kind, expected_names)


def has_expected_scope_text_caps(
    sync_root: Path,
    kind: InlineCapKind,
    entries: dict[str, LockEntry],
) -> bool:
    kind_dir = sync_root / section_name(kind)
    if not kind_dir.exists():
        return False
    expected_paths = {inline_cap_path(sync_root, kind, name, "md") for name in entries} | {
        inline_cap_meta_path(sync_root, kind, name) for name in entries
    }
    if set(kind_dir.iterdir()) != expected_paths:
        return False
    for name, entry in entries.items():
        meta_path = inline_cap_meta_path(sync_root, kind, name)
        if not meta_path.exists():
            return False
        meta = CapSidecar.model_validate_json(meta_path.read_text(encoding="utf-8"))
        if not _text_meta_matches_entry(meta, entry):
            return False
        raw_path = sync_root / meta.path
        if not raw_path.exists() or raw_path.read_text(encoding="utf-8") != meta.raw_text:
            return False
    return True


def has_expected_agent_text_caps(
    sync_root: Path,
    kind: InlineCapKind,
    entries: dict[str, LockEntry],
    inline_caps: list[CapContent],
) -> bool:
    kind_dir = sync_root / section_name(kind)
    if not kind_dir.exists():
        return False

    inline_by_name = {cap.name: cap for cap in inline_caps}
    expected_names = set(entries) | set(inline_by_name)
    expected_paths = {inline_cap_meta_path(sync_root, kind, name) for name in expected_names} | {
        inline_cap_path(
            sync_root,
            kind,
            name,
            inline_by_name[name].language if name in inline_by_name else "md",
        )
        for name in expected_names
    }
    if set(kind_dir.iterdir()) != expected_paths:
        return False

    for name in expected_names:
        meta = CapSidecar.model_validate_json(
            inline_cap_meta_path(sync_root, kind, name).read_text(encoding="utf-8")
        )
        raw_path = sync_root / meta.path
        if not raw_path.exists() or raw_path.read_text(encoding="utf-8") != meta.raw_text:
            return False
        inline_cap = inline_by_name.get(name)
        if inline_cap is not None:
            if (
                meta.language != inline_cap.language
                or meta.raw_text != inline_cap.raw_text
                or meta.params != inline_cap.params
            ):
                return False
            continue
        if not _text_meta_matches_entry(meta, entries[name]):
            return False
    return True


def _sync_locked_text_caps(
    sync_root: Path,
    kind: InlineCapKind,
    entries: dict[str, LockEntry],
    *,
    scope_source_root: Path,
) -> set[str]:
    expected_names: set[str] = set()
    for name, entry in entries.items():
        _sync_locked_text_cap(
            sync_root,
            kind,
            name,
            entry,
            scope_source_root=scope_source_root,
        )
        expected_names.add(name)
    return expected_names


def _sync_locked_text_cap(
    sync_root: Path,
    kind: InlineCapKind,
    name: str,
    entry: LockEntry,
    *,
    scope_source_root: Path,
) -> None:
    if entry.ref is None:
        sync_file_cap_materialization(
            sync_root,
            kind,
            name,
            scope_source_root / entry.path,
            source_path=entry.path,
        )
        return
    resolved = _resolved_text_ref(kind, name, entry)
    source_path, _ = remote.fetch_github_artifact(resolved)
    try:
        sync_file_cap_materialization(
            sync_root,
            kind,
            name,
            source_path,
            source_path=resolved.path,
            ref=resolved.ref,
            repo=resolved.repo,
            rev=resolved.rev,
        )
    finally:
        shutil.rmtree(source_path.parent.parent, ignore_errors=True)


def _resolved_text_ref(
    kind: InlineCapKind,
    name: str,
    entry: LockEntry,
) -> CapRef:
    return CapRef(
        kind=kind,
        name=name,
        ref=entry.ref or "",
        repo=entry.repo or "",
        path=entry.path,
        rev=entry.rev or "",
    )


def _text_meta_matches_entry(meta: CapSidecar, entry: LockEntry) -> bool:
    return (
        meta.ref == entry.ref
        and meta.repo == entry.repo
        and meta.source_path == entry.path
        and meta.rev == entry.rev
    )
