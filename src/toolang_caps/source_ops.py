from __future__ import annotations

import re
import shutil
from pathlib import Path

from toolang.errors import ToolangError
from toolang_caps.models import CapKind

USE_CAP_RE = re.compile(r"^\s*use\s+(skill|service|prompt|psyche)\s+(\S+)\s*$")


def cap_name_from_ref(ref: str) -> str:
    owner, sep, name = ref.partition("/")
    if not owner or not sep or not name:
        raise ToolangError(f"Capability ref must look like owner/name: {ref}")
    return name


def add_cap_ref(path: Path, kind: CapKind, ref: str) -> bool:
    name = cap_name_from_ref(ref)
    lines = _read_lines(path)
    existing = _cap_uses(lines)

    if any(existing_kind == kind and existing_ref == ref for _, existing_kind, existing_ref in existing):
        return False

    for _, existing_kind, existing_ref in existing:
        if existing_kind == kind and cap_name_from_ref(existing_ref) == name:
            raise ToolangError(
                f"{kind.title()} {name!r} is already referenced in {path} as {existing_ref!r}."
            )

    use_line = f"use {kind} {ref}"
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


def remove_cap_ref(
    path: Path,
    kind: CapKind,
    name: str,
    *,
    delete_when_empty: bool = False,
) -> bool:
    lines = _read_lines(path)
    if not lines:
        return False

    remove_indexes = {
        index
        for index, existing_kind, ref in _cap_uses(lines)
        if existing_kind == kind and cap_name_from_ref(ref) == name
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


def local_cap_path(kind_dir: Path, kind: CapKind, name: str) -> Path:
    if kind == "skill":
        return kind_dir / name
    return kind_dir / f"{name}.md"


def create_local_cap(target: Path, kind: CapKind, name: str) -> None:
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
    if not target.exists():
        return False
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return True


def prune_empty_local_kind_dir(kind_dir: Path) -> None:
    if kind_dir.exists() and not any(kind_dir.iterdir()):
        kind_dir.rmdir()


def _default_cap_body(kind: CapKind, name: str) -> str:
    title = name.replace("-", " ").title()
    if kind == "service":
        return (
            "---\n"
            "transport: http\n"
            "target: https://example.com\n"
            f"description: {title} service\n"
            "---\n\n"
            "Describe how and when to use this service.\n"
        )
    if kind == "prompt":
        return f"Write the {title} prompt here.\n\n{{{{input}}}}\n"
    if kind == "psyche":
        return f"Describe the {title} behavior here.\n"
    raise ToolangError(f"Unsupported local cap kind: {kind}")


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


def _cap_uses(lines: list[str]) -> list[tuple[int, str, str]]:
    uses: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        match = USE_CAP_RE.match(line)
        if match is not None:
            uses.append((index, match.group(1), match.group(2)))
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
