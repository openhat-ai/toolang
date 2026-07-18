"""Toolang CLI version helpers."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
import subprocess
import tomllib


def toolang_version() -> str:
    return f"{base_toolang_version()}{source_state_suffix()}"


def base_toolang_version() -> str:
    try:
        return package_version("toolang")
    except PackageNotFoundError:
        pass
    for parent in Path(__file__).resolve().parents:
        pyproject_path = parent / "pyproject.toml"
        if not pyproject_path.is_file():
            continue
        try:
            data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return "unknown"
        project = data.get("project")
        if not isinstance(project, dict):
            return "unknown"
        version = project.get("version")
        return version if isinstance(version, str) else "unknown"
    return "unknown"


def source_state_suffix() -> str:
    source_root = source_tree_root()
    if source_root is None:
        return ""
    short_sha = git_output(source_root, "rev-parse", "--short", "HEAD")
    if short_sha is None:
        return ""
    dirty = git_output(source_root, "status", "--short")
    if dirty is None:
        return f"+{short_sha}"
    dirty_suffix = "*" if dirty else ""
    return f"+{short_sha}{dirty_suffix}"


def git_output(source_root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=source_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def source_tree_root() -> Path | None:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / ".git").exists():
            return parent
    return None
