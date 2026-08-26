"""Build-time source provenance for Toolang distributions."""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any

from dulwich import porcelain
from dulwich.errors import NotGitRepository, ObjectMissing
from dulwich.repo import Repo
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


_BUILD_INFO_NAME = "_build_info.json"
_UNKNOWN_SOURCE_VERSION = "unknown"


def collect_build_info(root: Path) -> dict[str, object]:
    """Return inherited or live source provenance for one artifact build."""

    inherited_path = root / "src" / "toolang" / _BUILD_INFO_NAME
    if inherited_path.is_file():
        return read_build_info(inherited_path)

    repository_info = _repository_build_info(root)
    if repository_info is None:
        return _unknown_build_info()
    source_version, revision, dirty = repository_info
    return {
        "schema": 1,
        "source_version": source_version,
        "revision": revision,
        "dirty": dirty,
    }


def read_build_info(path: Path) -> dict[str, object]:
    """Read and validate inherited build information."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid Toolang build info at {path}") from error
    if not isinstance(payload, Mapping):
        raise ValueError(f"invalid Toolang build info at {path}")

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
        raise ValueError(f"invalid Toolang build info at {path}")
    return {
        "schema": schema,
        "source_version": source_version,
        "revision": revision,
        "dirty": dirty,
    }


def _unknown_build_info() -> dict[str, object]:
    return {
        "schema": 1,
        "source_version": _UNKNOWN_SOURCE_VERSION,
        "revision": None,
        "dirty": None,
    }


def _repository_build_info(root: Path) -> tuple[str, str, bool] | None:
    try:
        with Repo.discover(root) as repository:
            if Path(repository.path).resolve() != root.resolve():
                return None
            source_version = porcelain.describe(repository, abbrev=8)
            status = porcelain.status(repository, untracked_files="no")
            dirty = any(status.staged.values()) or bool(status.unstaged)
            if dirty:
                source_version = f"{source_version}*"
            return source_version, repository.head().decode("ascii"), dirty
    except (KeyError, NotGitRepository, ObjectMissing, OSError, ValueError):
        return None


class CustomBuildHook(BuildHookInterface):
    """Inject source provenance into standard sdist and wheel artifacts."""

    _temporary_directory: Path | None = None

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version.startswith("editable"):
            return

        build_info = collect_build_info(Path(self.root))
        temporary_directory = Path(tempfile.mkdtemp(prefix="toolang-build-info-"))
        self._temporary_directory = temporary_directory
        generated_path = temporary_directory / _BUILD_INFO_NAME
        generated_path.write_text(
            f"{json.dumps(build_info, indent=2, sort_keys=True)}\n",
            encoding="utf-8",
        )
        target_path = (
            f"toolang/{_BUILD_INFO_NAME}"
            if self.target_name == "wheel"
            else f"src/toolang/{_BUILD_INFO_NAME}"
        )
        build_data["force_include"][str(generated_path)] = target_path

    def finalize(
        self,
        version: str,
        build_data: dict[str, Any],
        artifact_path: str,
    ) -> None:
        del version, build_data, artifact_path
        if self._temporary_directory is not None:
            shutil.rmtree(self._temporary_directory, ignore_errors=True)
            self._temporary_directory = None
