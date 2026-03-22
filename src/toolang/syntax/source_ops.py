"""Authored source editing helpers for Toolang programs."""

from __future__ import annotations

from pathlib import Path

from toolang.errors import ToolangError
from toolang.concepts.caps import CapKind

from .parser import parse_program


def cap_name_from_ref(ref: str) -> str:
    """Return the capability name portion of one owner/name ref."""

    owner, sep, name = ref.partition("/")
    if not owner or not sep or not name:
        raise ToolangError(f"Capability ref must look like owner/name: {ref}")
    return name


def add_cap_ref(path: Path, kind: CapKind, ref: str) -> bool:
    """Add one `use <kind> <ref>` statement while preserving surrounding source."""

    name = cap_name_from_ref(ref)
    lines = _read_lines(path)
    program = parse_program("\n".join(lines)) if lines else None

    if program is not None and program.has_use(kind, ref):
        return False

    if program is not None:
        for use in program.uses_by_kind(kind):
            if cap_name_from_ref(use.reference) == name:
                raise ToolangError(
                    f"{kind.title()} {name!r} is already referenced in {path} as {use.reference!r}."
                )

    use_line = f"use {kind} {ref}"
    if not lines:
        _write_lines(path, [use_line])
        return True

    use_indexes = [index for index, line in enumerate(lines) if _is_cap_use_line(line)]
    insert_at = use_indexes[-1] + 1 if use_indexes else _leading_header_length(lines)

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
    """Remove `use` statements for one capability name while preserving other source."""

    lines = _read_lines(path)
    if not lines:
        return False

    program = parse_program("\n".join(lines))
    remove_indexes = {
        use.span.line - 1
        for use in program.uses_by_kind(kind)
        if cap_name_from_ref(use.reference) == name
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


def _is_cap_use_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("use ") and len(stripped.split()) == 3


def _leading_header_length(lines: list[str]) -> int:
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        break
    return index
