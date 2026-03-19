from __future__ import annotations

import shutil
from pathlib import Path

from toolang_caps.frontmatter import parse_cap_body
from toolang_caps.models import (
    CAP_KINDS,
    InlineCap,
    InlineCapKind,
    InlineCapMeta,
    SkillMeta,
    section_name,
)

LANGUAGE_EXTENSIONS = {
    "json": ".json",
    "md": ".md",
    "python": ".py",
}


def sync_inline_caps(root: Path, caps: list[InlineCap]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for kind in CAP_KINDS:
        (root / section_name(kind)).mkdir(parents=True, exist_ok=True)

    expected: dict[Path, str] = {}
    for cap in caps:
        raw_path = inline_cap_path(root, cap.kind, cap.name, cap.language)
        meta_path = inline_cap_meta_path(root, cap.kind, cap.name)
        parsed = parse_cap_body(cap.language, cap.raw_text)

        raw_path.write_text(cap.raw_text, encoding="utf-8")
        meta = InlineCapMeta(
            kind=cap.kind,
            name=cap.name,
            language=cap.language,
            path=str(raw_path.relative_to(root)),
            params=cap.params,
            front_matter=parsed.front_matter,
            content=parsed.content,
        )
        meta_path.write_text(
            meta.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )
        expected[raw_path] = "file"
        expected[meta_path] = "file"

    for kind in ("service", "prompt", "psyche"):
        kind_dir = root / section_name(kind)
        for existing in kind_dir.iterdir():
            if existing not in expected:
                _remove_path(existing)


def sync_skill_materialization(root: Path, name: str, source_dir: Path, resolved, files: list[str]) -> None:
    skill_dir = skill_cap_dir(root, name)
    meta_path = skill_cap_meta_path(root, name)
    _remove_path(skill_dir)
    shutil.copytree(source_dir, skill_dir)
    meta = SkillMeta(
        name=name,
        path=str(skill_dir.relative_to(root)) + "/",
        entry_path=str((skill_dir / "SKILL.md").relative_to(root)),
        files=sorted(files),
        ref=resolved.ref,
        repo=resolved.repo,
        source_path=resolved.path,
        rev=resolved.rev,
    )
    meta_path.write_text(meta.model_dump_json(indent=2), encoding="utf-8")


def remove_stale_skill_materializations(root: Path, expected_names: set[str]) -> None:
    kind_dir = root / section_name("skill")
    kind_dir.mkdir(parents=True, exist_ok=True)
    expected_paths = {
        skill_cap_dir(root, name) for name in expected_names
    } | {
        skill_cap_meta_path(root, name) for name in expected_names
    }
    for existing in kind_dir.iterdir():
        if existing not in expected_paths:
            _remove_path(existing)


def inline_cap_path(root: Path, kind: InlineCapKind, name: str, language: str | None) -> Path:
    extension = LANGUAGE_EXTENSIONS.get(language or "", f".{language}" if language else ".txt")
    return root / section_name(kind) / f"{name}{extension}"


def inline_cap_meta_path(root: Path, kind: InlineCapKind, name: str) -> Path:
    return root / section_name(kind) / f"{name}.meta.json"


def skill_cap_dir(root: Path, name: str) -> Path:
    return root / section_name("skill") / name


def skill_cap_meta_path(root: Path, name: str) -> Path:
    return root / section_name("skill") / f"{name}.meta.json"


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink()
