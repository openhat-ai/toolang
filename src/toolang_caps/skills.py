from __future__ import annotations

import re
import shutil
from pathlib import Path

from toolang.errors import ToolangError

USE_SKILL_RE = re.compile(r"^\s*use\s+skill\s+(\S+)\s*$")


def skill_name_from_ref(ref: str) -> str:
    owner, sep, name = ref.partition("/")
    if not owner or not sep or not name:
        raise ToolangError(f"Skill ref must look like owner/name: {ref}")
    return name


def add_skill_ref(path: Path, ref: str) -> bool:
    name = skill_name_from_ref(ref)
    lines = _read_lines(path)
    existing = _skill_uses(lines)

    if any(existing_ref == ref for _, existing_ref in existing):
        return False

    for _, existing_ref in existing:
        if skill_name_from_ref(existing_ref) == name:
            raise ToolangError(
                f"Skill {name!r} is already referenced in {path} as {existing_ref!r}."
            )

    use_line = f"use skill {ref}"
    if not lines:
        _write_lines(path, [use_line])
        return True

    if existing:
        insert_at = existing[-1][0] + 1
    else:
        insert_at = _leading_header_length(lines)

    updated = list(lines)
    updated.insert(insert_at, use_line)
    if insert_at == 0 and len(updated) > 1 and updated[1].strip():
        updated.insert(1, "")
    _write_lines(path, updated)
    return True


def remove_skill_ref(path: Path, name: str, *, delete_when_empty: bool = False) -> bool:
    lines = _read_lines(path)
    if not lines:
        return False

    remove_indexes = {
        index
        for index, ref in _skill_uses(lines)
        if skill_name_from_ref(ref) == name
    }
    if not remove_indexes:
        return False

    updated = [line for index, line in enumerate(lines) if index not in remove_indexes]
    while updated and not updated[0].strip():
        updated.pop(0)
    while len(updated) >= 2 and not updated[0].strip() and not updated[1].strip():
        updated.pop(0)
    while len(updated) >= 2 and not updated[-1].strip() and not updated[-2].strip():
        updated.pop()

    if updated:
        _write_lines(path, updated)
    elif delete_when_empty and path.exists():
        path.unlink()
    else:
        _write_lines(path, [])
    return True


def create_local_skill(target_dir: Path, name: str) -> None:
    if target_dir.exists():
        raise ToolangError(f"Local skill already exists: {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=False)
    (target_dir / "SKILL.md").write_text(
        f"# {name.replace('-', ' ').title()}\n\nDescribe this skill.\n",
        encoding="utf-8",
    )


def install_local_skill(target_dir: Path, source_dir: Path) -> None:
    if target_dir.exists():
        raise ToolangError(f"Local skill already exists: {target_dir}")
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, target_dir)


def delete_local_skill(target_dir: Path) -> bool:
    if not target_dir.exists():
        return False
    shutil.rmtree(target_dir)
    return True


def prune_empty_local_kind_dir(kind_dir: Path) -> None:
    if kind_dir.exists() and not any(kind_dir.iterdir()):
        kind_dir.rmdir()


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not lines:
        path.write_text("", encoding="utf-8")
        return
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _skill_uses(lines: list[str]) -> list[tuple[int, str]]:
    uses: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = USE_SKILL_RE.match(line)
        if match is not None:
            uses.append((index, match.group(1)))
    return uses


def _leading_header_length(lines: list[str]) -> int:
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        break
    return index
