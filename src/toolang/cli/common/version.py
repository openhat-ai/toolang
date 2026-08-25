"""Toolang CLI version helpers."""

from __future__ import annotations

from importlib.metadata import distribution as package_distribution
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
import json
from pathlib import Path
import subprocess
import tomllib
from urllib.parse import urlsplit
from urllib.request import url2pathname


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


def development_source() -> tuple[bool, Path | None]:
    """Return whether Toolang runs from development source and its local path."""

    try:
        distribution = package_distribution("toolang")
    except PackageNotFoundError:
        source_root = source_project_root()
        return source_root is not None, source_root
    try:
        raw = distribution.read_text("direct_url.json")
    except OSError:
        return False, None
    if raw is None:
        return False, None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False, None
    if not isinstance(payload, dict):
        return False, None
    directory = payload.get("dir_info")
    if not isinstance(directory, dict) or directory.get("editable") is not True:
        return False, None
    url = payload.get("url")
    return True, _local_file_url(url) if isinstance(url, str) else None


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


def source_project_root() -> Path | None:
    """Return the nearest source project when it is Toolang."""

    current = Path(__file__).resolve()
    for parent in current.parents:
        pyproject_path = parent / "pyproject.toml"
        if not pyproject_path.is_file():
            continue
        try:
            data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return None
        project = data.get("project")
        if not isinstance(project, dict):
            return None
        name = project.get("name")
        return (
            parent if isinstance(name, str) and name.casefold() == "toolang" else None
        )
    return None


def _local_file_url(value: str) -> Path | None:
    parsed = urlsplit(value)
    if (
        parsed.scheme.casefold() != "file"
        or parsed.netloc not in {"", "localhost"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        return Path(url2pathname(parsed.path)).resolve()
    except OSError:
        return None
