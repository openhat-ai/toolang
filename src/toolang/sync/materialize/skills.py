from __future__ import annotations

import shutil
from pathlib import Path

from toolang.files.sync_state import LockEntry
from toolang_caps.files import (
    remove_stale_skill_materializations,
    skill_cap_dir,
    skill_cap_meta_path,
    sync_local_skill_materialization,
    sync_skill_materialization,
)
from toolang_caps.models import CapRef, CapSidecar, section_name

from .. import remote


def sync_scope_skills(
    sync_root: Path,
    entries: dict[str, LockEntry],
    *,
    scope_source_root: Path,
) -> None:
    expected_names: set[str] = set()
    for name, entry in entries.items():
        if entry.ref is None:
            source_dir = scope_source_root / entry.path
            sync_local_skill_materialization(
                sync_root,
                name,
                source_dir,
                files=skill_files(source_dir),
                source_path=entry.path,
            )
        else:
            resolved = _resolved_skill_ref(name, entry)
            source_dir, files = remote.fetch_github_artifact(resolved)
            try:
                sync_skill_materialization(sync_root, name, source_dir, resolved, files)
            finally:
                shutil.rmtree(source_dir.parent.parent, ignore_errors=True)
        expected_names.add(name)
    remove_stale_skill_materializations(sync_root, expected_names)


def has_expected_scope_skills(
    sync_root: Path,
    entries: dict[str, LockEntry],
) -> bool:
    kind_dir = sync_root / section_name("skill")
    if not kind_dir.exists():
        return False

    expected_top_level = {skill_cap_dir(sync_root, name) for name in entries} | {
        skill_cap_meta_path(sync_root, name) for name in entries
    }
    if set(kind_dir.iterdir()) != expected_top_level:
        return False

    for name, entry in entries.items():
        skill_dir = skill_cap_dir(sync_root, name)
        meta_path = skill_cap_meta_path(sync_root, name)
        if not skill_dir.exists() or not meta_path.exists():
            return False
        meta = CapSidecar.model_validate_json(meta_path.read_text(encoding="utf-8"))
        if (
            meta.ref != entry.ref
            or meta.repo != entry.repo
            or meta.source_path != entry.path
            or meta.rev != entry.rev
        ):
            return False
        actual_files = sorted(
            str(path.relative_to(skill_dir))
            for path in skill_dir.rglob("*")
            if path.is_file()
        )
        if actual_files != meta.asset_files:
            return False
    return True


def skill_files(source_dir: Path) -> list[str]:
    return sorted(
        str(path.relative_to(source_dir))
        for path in source_dir.rglob("*")
        if path.is_file()
    )


def _resolved_skill_ref(name: str, entry: LockEntry) -> CapRef:
    return CapRef(
        kind="skill",
        name=name,
        ref=entry.ref or "",
        repo=entry.repo or "",
        path=entry.path,
        rev=entry.rev or "",
    )
