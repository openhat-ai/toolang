from __future__ import annotations

from pathlib import Path
from typing import cast, get_args

from toolang.errors import ToolangError
from toolang.syntax import Program
from toolang.concepts.caps import (
    CapContent,
    CapKind,
    CapParam,
)
from toolang.concepts.persisted.sync_state import LockEntry, LockedAgentRefs

from . import remote

ALL_CAP_KINDS = get_args(CapKind)
REFS_ATTR_BY_KIND: dict[CapKind, str] = {
    "skill": "skills",
    "service": "services",
    "prompt": "prompts",
    "psyche": "psyches",
}

SOURCE_DECL_TO_CAP_KIND: dict[str, CapKind] = {
    "service": "service",
    "prompt": "prompt",
    "psyche": "psyche",
}


def agent_declared_caps(program: Program) -> list[CapContent]:
    caps: list[CapContent] = []
    for declaration in program.declarations:
        kind = SOURCE_DECL_TO_CAP_KIND.get(declaration.kind)
        if kind is None:
            continue
        caps.append(
            CapContent(
                kind=kind,
                name=declaration.name,
                language=declaration.language,
                raw_text=declaration.body,
                params=[
                    CapParam(name=param.name, optional=param.optional)
                    for param in declaration.params
                ],
            )
        )
    return caps


def load_scope_refs(path: Path, *, scope_label: str) -> LockedAgentRefs:
    if not path.exists():
        return LockedAgentRefs()

    from toolang.syntax import parse_program

    program = parse_program(path.read_text(encoding="utf-8"))
    if program.declarations or program.thunks:
        raise ToolangError(f"{scope_label} may only contain 'use ...' statements.")

    refs = LockedAgentRefs()
    for use in program.uses:
        if use.kind not in ALL_CAP_KINDS:
            raise ToolangError(f"Unsupported cap kind in {scope_label}: {use.kind}")
        kind = cast(CapKind, use.kind)
        resolved = remote.resolve_github_cap_ref(kind, use.reference)
        entries = entries_for_kind(refs, kind)
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
    return sorted_entries(refs)


def resolve_home_refs(programs: dict[str, Program]) -> dict[str, LockedAgentRefs]:
    refs_by_agent: dict[str, LockedAgentRefs] = {}
    for agent_name, program in sorted(programs.items()):
        refs = LockedAgentRefs()
        for use in program.uses:
            if use.kind not in ALL_CAP_KINDS:
                raise ToolangError(
                    f"Unsupported capability ref kind in {agent_name}.too: {use.kind}."
                )
            kind = cast(CapKind, use.kind)
            resolved = remote.resolve_github_cap_ref(kind, use.reference)
            entries = entries_for_kind(refs, kind)
            entry = LockEntry(
                ref=resolved.ref,
                repo=resolved.repo,
                path=resolved.path,
                rev=resolved.rev,
            )
            existing = entries.get(resolved.name)
            if existing is not None and existing != entry:
                raise ToolangError(
                    f"Conflicting {use.kind} refs resolve to the same name in {agent_name}.too: {resolved.name}"
                )
            entries[resolved.name] = entry
        refs_by_agent[agent_name] = sorted_entries(refs)
    return refs_by_agent


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
        entries = entries_for_kind(refs, kind)
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
    return sorted_entries(refs)


def overlay_ref_entries(refs: LockedAgentRefs, locals_by_name: LockedAgentRefs) -> LockedAgentRefs:
    effective = LockedAgentRefs()
    for kind in ALL_CAP_KINDS:
        merged = dict(entries_for_kind(refs, kind))
        merged.update(entries_for_kind(locals_by_name, kind))
        setattr(
            effective,
            REFS_ATTR_BY_KIND[kind],
            {name: merged[name] for name in sorted(merged)},
        )
    return effective


def entries_for_kind(refs: LockedAgentRefs, kind: CapKind) -> dict[str, LockEntry]:
    return getattr(refs, REFS_ATTR_BY_KIND[kind])


def sorted_entries(refs: LockedAgentRefs) -> LockedAgentRefs:
    sorted_refs = LockedAgentRefs()
    for kind in ALL_CAP_KINDS:
        entries = entries_for_kind(refs, kind)
        setattr(
            sorted_refs,
            REFS_ATTR_BY_KIND[kind],
            {name: entries[name] for name in sorted(entries)},
        )
    return sorted_refs
