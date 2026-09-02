"""Current-agent authored-storage safety checks."""

from __future__ import annotations

from pathlib import Path

from toolang.catalog.types import CAP_DIR_BY_KIND, CapKind

_JOB_DIRECTORIES = (
    Path("tasks"),
    Path("chores"),
    Path("drafts/tasks"),
    Path("drafts/chores"),
    Path("archive/tasks"),
    Path("archive/chores"),
)


class UnsafeAuthoringPathError(ValueError):
    """An authored storage path could escape or has an unsupported shape."""


def validate_job_storage(home: Path, *, allocator: bool) -> None:
    """Reject unsafe job, lock, and optional allocator storage."""

    require_regular_file(home / ".authored-jobs.lock", "job lock")
    for relative in _JOB_DIRECTORIES:
        directory = require_directory(home, relative, "job storage")
        if directory is None:
            continue
        for path in directory.iterdir():
            if path.suffix == ".md":
                require_regular_file(path, "job document")
    if allocator:
        require_directory(home, Path(".runtime"), "runtime storage")
        require_regular_file(home / ".runtime" / "ids.json", "id allocator")


def validate_cap_storage(
    home: Path,
    kind: CapKind,
    *,
    key: str | None = None,
    recursive: bool = False,
) -> None:
    """Reject unsafe cap storage before an authored catalog can follow it."""

    require_regular_file(home / ".authored-caps.lock", "cap lock")
    relative = Path(CAP_DIR_BY_KIND[kind])
    directory = require_directory(home, relative, f"{kind} storage")
    if directory is None:
        return
    if key is None:
        _validate_cap_listing(directory, kind)
        return
    if kind == "skill":
        skill_directory = require_directory(
            home,
            relative / key,
            "skill directory",
        )
        if skill_directory is None:
            return
        require_regular_file(skill_directory / "SKILL.md", "skill definition")
        if recursive:
            _validate_tree(skill_directory)
            require_directory(home, Path(".runtime"), "runtime storage")
            require_directory(
                home,
                Path(".runtime/authored-cap-trash"),
                "authored cap trash",
            )
        return
    require_regular_file(directory / f"{key}.md", f"{kind} definition")


def require_directory(home: Path, relative: Path, label: str) -> Path | None:
    """Validate one relative directory path without following symlinks."""

    current = home
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise UnsafeAuthoringPathError(f"{label} must not be a symbolic link")
        if not current.exists():
            return None
        if not current.is_dir():
            raise UnsafeAuthoringPathError(f"{label} must be a directory")
    return current


def require_regular_file(path: Path, label: str) -> None:
    """Require an existing node to be a non-symlink regular file."""

    if path.is_symlink():
        raise UnsafeAuthoringPathError(f"{label} must not be a symbolic link")
    if path.exists() and not path.is_file():
        raise UnsafeAuthoringPathError(f"{label} must be a regular file")


def _validate_cap_listing(directory: Path, kind: CapKind) -> None:
    if kind == "skill":
        for path in directory.iterdir():
            if path.is_symlink():
                raise UnsafeAuthoringPathError(
                    "skill directory must not be a symbolic link"
                )
            if not path.is_dir():
                continue
            require_regular_file(path / "SKILL.md", "skill definition")
        return
    for path in directory.iterdir():
        if path.suffix == ".md":
            require_regular_file(path, f"{kind} definition")


def _validate_tree(directory: Path) -> None:
    pending = [directory]
    while pending:
        current = pending.pop()
        for path in current.iterdir():
            if path.is_symlink():
                raise UnsafeAuthoringPathError(
                    "skill contents must not contain symbolic links"
                )
            if path.is_dir():
                pending.append(path)
            elif not path.is_file():
                raise UnsafeAuthoringPathError(
                    "skill contents must contain only regular files"
                )
