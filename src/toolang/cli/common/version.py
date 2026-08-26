"""Toolang CLI version helpers."""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from importlib.metadata import distribution as package_distribution
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
import json
from pathlib import Path
import tomllib
from urllib.parse import urlsplit
from urllib.request import url2pathname

_BUILD_INFO_NAME = "_build_info.json"
_UNKNOWN_SOURCE_VERSION = "unknown"


@cache
def toolang_version() -> str:
    """Return the source version captured at process startup."""

    detected, source = development_source()
    if detected:
        source_root = source if source is not None else source_project_root()
        if source_root is None:
            return _UNKNOWN_SOURCE_VERSION
        return repository_source_version(source_root) or _UNKNOWN_SOURCE_VERSION
    return embedded_source_version()


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


def embedded_source_version() -> str:
    """Return validated source provenance from an installed artifact."""

    path = Path(__file__).resolve().parents[2] / _BUILD_INFO_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _UNKNOWN_SOURCE_VERSION
    if not isinstance(payload, Mapping):
        return _UNKNOWN_SOURCE_VERSION

    schema = payload.get("schema")
    source_version = payload.get("source_version")
    revision = payload.get("revision")
    dirty = payload.get("dirty")
    if (
        type(schema) is not int
        or schema != 1
        or not isinstance(source_version, str)
        or not source_version
        or (revision is not None and not isinstance(revision, str))
        or (dirty is not None and type(dirty) is not bool)
    ):
        return _UNKNOWN_SOURCE_VERSION
    return source_version


def repository_source_version(source_root: Path) -> str | None:
    """Describe tracked source state without requiring a Git executable."""

    from dulwich import porcelain
    from dulwich.errors import NotGitRepository, ObjectMissing
    from dulwich.repo import Repo

    try:
        with Repo.discover(source_root) as repository:
            source_version = porcelain.describe(repository, abbrev=8)
            status = porcelain.status(repository, untracked_files="no")
            if any(status.staged.values()) or status.unstaged:
                return f"{source_version}*"
            return source_version
    except (KeyError, NotGitRepository, ObjectMissing, OSError, ValueError):
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
