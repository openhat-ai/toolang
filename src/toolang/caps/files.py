"""Capability materialization helpers for synced local artifacts."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

from toolang.concepts.caps import (
    CapContent,
    CapKind,
    CapParam,
    CapSidecar,
    parse_cap_body,
)
from toolang.errors import ToolangError

from ._paths import section_dir_name

LANGUAGE_EXTENSIONS = {
    "json": ".json",
    "md": ".md",
    "python": ".py",
}
DECLARED_CAP_KINDS: tuple[Literal["service", "prompt", "psyche"], ...] = (
    "service",
    "prompt",
    "psyche",
)


def sync_declared_caps(root: Path, caps: list[CapContent]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for kind in (*DECLARED_CAP_KINDS, "skill"):
        (root / section_dir_name(kind)).mkdir(parents=True, exist_ok=True)

    expected_by_kind: dict[CapKind, set[str]] = {kind: set() for kind in DECLARED_CAP_KINDS}
    for cap in caps:
        sync_declared_cap_materialization(
            root,
            cap.kind,
            cap.name,
            cap.raw_text,
            language=cap.language,
            params=cap.params,
        )
        names = expected_by_kind.get(cap.kind)
        if names is None:
            raise ValueError(f"Unsupported declared cap kind: {cap.kind}")
        names.add(cap.name)

    for kind in DECLARED_CAP_KINDS:
        remove_stale_declared_cap_materializations(root, kind, expected_by_kind[kind])


def sync_declared_cap_materialization(
    root: Path,
    kind: CapKind,
    name: str,
    raw_text: str,
    *,
    language: str | None,
    params: list[CapParam] | None = None,
    ref: str | None = None,
    repo: str | None = None,
    source_path: str | None = None,
    rev: str | None = None,
) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    (root / section_dir_name(kind)).mkdir(parents=True, exist_ok=True)
    raw_path = declared_cap_path(root, kind, name, language)
    meta_path = declared_cap_meta_path(root, kind, name)
    parsed = parse_cap_body(kind, language, raw_text)

    raw_path.write_text(raw_text, encoding="utf-8")
    meta = CapSidecar(
        kind=kind,
        name=name,
        language=language,
        path=str(raw_path.relative_to(root)),
        params=list(params or []),
        front_matter=parsed.front_matter,
        content=parsed.content,
        raw_text=raw_text,
        ref=ref,
        repo=repo,
        source_path=source_path,
        rev=rev,
    )
    meta_path.write_text(
        meta.model_dump_json(indent=2, exclude_none=True),
        encoding="utf-8",
    )
    return raw_path, meta_path


def sync_file_cap_materialization(
    root: Path,
    kind: CapKind,
    name: str,
    source_file: Path,
    *,
    source_path: str,
    ref: str | None = None,
    repo: str | None = None,
    rev: str | None = None,
) -> None:
    sync_declared_cap_materialization(
        root,
        kind,
        name,
        source_file.read_text(encoding="utf-8"),
        language=_language_from_path(source_file),
        ref=ref,
        repo=repo,
        source_path=source_path,
        rev=rev,
    )


def sync_skill_materialization(root: Path, name: str, source_dir: Path, resolved, files: list[str]) -> None:
    sync_local_skill_materialization(
        root,
        name,
        source_dir,
        files=files,
        source_path=resolved.path,
        ref=resolved.ref,
        repo=resolved.repo,
        rev=resolved.rev,
    )


def sync_local_skill_materialization(
    root: Path,
    name: str,
    source_dir: Path,
    *,
    files: list[str],
    source_path: str,
    ref: str | None = None,
    repo: str | None = None,
    rev: str | None = None,
) -> None:
    skill_dir = skill_cap_dir(root, name)
    meta_path = skill_cap_meta_path(root, name)
    skill_text = (source_dir / "SKILL.md").read_text(encoding="utf-8")
    parsed = parse_cap_body("skill", "md", skill_text)
    _remove_path(skill_dir)
    shutil.copytree(source_dir, skill_dir)
    meta = CapSidecar(
        kind="skill",
        name=name,
        path=str(skill_dir.relative_to(root)) + "/",
        language="md",
        front_matter=parsed.front_matter,
        content=parsed.content,
        raw_text=skill_text,
        entry_path=str((skill_dir / "SKILL.md").relative_to(root)),
        asset_files=sorted(files),
        ref=ref,
        repo=repo,
        source_path=source_path,
        rev=rev,
    )
    meta_path.write_text(meta.model_dump_json(indent=2), encoding="utf-8")


def local_cap_path(kind_dir: Path, kind: CapKind, name: str) -> Path:
    """Return the authored local capability path for one scope and kind."""

    if kind == "skill":
        return kind_dir / name
    return kind_dir / f"{name}.md"


def create_local_cap(target: Path, kind: CapKind, name: str) -> None:
    """Create one new local authored capability."""

    if target.exists():
        raise ToolangError(f"Local {kind} already exists: {target}")
    if kind == "skill":
        target.mkdir(parents=True, exist_ok=False)
        (target / "SKILL.md").write_text(
            f"# {name.replace('-', ' ').title()}\n\nDescribe this skill.\n",
            encoding="utf-8",
        )
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_default_cap_body(kind, name), encoding="utf-8")


def install_local_cap(target: Path, kind: CapKind, source_path: Path) -> None:
    """Install one local capability from an existing authored source."""

    if target.exists():
        raise ToolangError(f"Local {kind} already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    if kind == "skill":
        if not source_path.is_dir():
            raise ToolangError(f"Expected a skill directory, got {source_path}")
        shutil.copytree(source_path, target)
        return

    if not source_path.is_file():
        raise ToolangError(f"Expected a {kind} file, got {source_path}")
    shutil.copy2(source_path, target)


def delete_local_cap(target: Path) -> bool:
    """Delete one local authored capability path if it exists."""

    if not target.exists():
        return False
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return True


def prune_empty_local_kind_dir(kind_dir: Path) -> None:
    """Remove one empty local-kind directory after deleting a capability."""

    if kind_dir.exists() and not any(kind_dir.iterdir()):
        kind_dir.rmdir()


def remove_stale_declared_cap_materializations(
    root: Path,
    kind: CapKind,
    expected_names: set[str],
) -> None:
    kind_dir = root / section_dir_name(kind)
    kind_dir.mkdir(parents=True, exist_ok=True)
    for existing in kind_dir.iterdir():
        if existing.name.endswith(".meta.json"):
            if existing.stem.removesuffix(".meta") not in expected_names:
                _remove_path(existing)
            continue
        if existing.stem not in expected_names:
            _remove_path(existing)


def remove_stale_skill_materializations(root: Path, expected_names: set[str]) -> None:
    kind_dir = root / section_dir_name("skill")
    kind_dir.mkdir(parents=True, exist_ok=True)
    expected_paths = {
        skill_cap_dir(root, name) for name in expected_names
    } | {
        skill_cap_meta_path(root, name) for name in expected_names
    }
    for existing in kind_dir.iterdir():
        if existing not in expected_paths:
            _remove_path(existing)


def declared_cap_path(root: Path, kind: CapKind, name: str, language: str | None) -> Path:
    extension = LANGUAGE_EXTENSIONS.get(language or "", f".{language}" if language else ".txt")
    return root / section_dir_name(kind) / f"{name}{extension}"


def declared_cap_meta_path(root: Path, kind: CapKind, name: str) -> Path:
    return root / section_dir_name(kind) / f"{name}.meta.json"


def skill_cap_dir(root: Path, name: str) -> Path:
    return root / section_dir_name("skill") / name


def skill_cap_meta_path(root: Path, name: str) -> Path:
    return root / section_dir_name("skill") / f"{name}.meta.json"


def _language_from_path(path: Path) -> str | None:
    suffix = path.suffix.lstrip(".")
    if not suffix:
        return None
    return suffix


def _default_cap_body(kind: CapKind, name: str) -> str:
    title = name.replace("-", " ").title()
    if kind == "service":
        return (
            "---\n"
            "transport: http\n"
            "target: https://example.com\n"
            f"description: {title} service\n"
            "env:\n"
            "  - SERVICE_TOKEN\n"
            "auth_env: SERVICE_TOKEN\n"
            "---\n\n"
            "Describe how and when to use this service.\n"
        )
    if kind == "prompt":
        return f"Write the {title} prompt here.\n\n{{{{input}}}}\n"
    if kind == "psyche":
        return f"Describe the {title} behavior here.\n"
    raise ToolangError(f"Unsupported local cap kind: {kind}")


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink()
