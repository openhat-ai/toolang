"""Select the controlling Toolang package for a new guest."""

from __future__ import annotations

from hashlib import sha256
from importlib.util import find_spec
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
from zipfile import BadZipFile, ZipFile

from toolang.base.types.progress import ProgressSink
from toolang.common.files import file_write_lock
from toolang.common.progress import emit_progress
from toolang.common.version import (
    development_source,
    repository_source_version,
    toolang_requirement,
)

_BUILD_FILES = (
    "pyproject.toml",
    "hatch_build.py",
    "README.md",
    "LICENSE",
    ".gitignore",
)


def resolve_guest_package(
    artifact: Path | None,
    *,
    cache: Path,
    progress: ProgressSink | None = None,
    progress_id: str = "guest-package",
) -> tuple[str | None, Path | None]:
    """Resolve an exact index requirement or a concrete local wheel."""

    if artifact is not None:
        return None, artifact
    editable, source = development_source()
    if not editable:
        return toolang_requirement(), None
    if source is None:
        raise ValueError(
            "Cannot locate the Toolang development source; use --dev PATH "
            "with a Toolang wheel."
        )
    return None, build_source_wheel(
        source, cache=cache, progress=progress, progress_id=progress_id
    )


def build_source_wheel(
    source: Path,
    *,
    cache: Path,
    progress: ProgressSink | None = None,
    progress_id: str = "guest-package",
) -> Path:
    """Build once for each set of source inputs, outside the source checkout."""

    source = source.resolve()
    project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    if (
        project.get("project", {}).get("name") != "toolang"
        or not (source / "src" / "toolang" / "__init__.py").is_file()
    ):
        raise ValueError(f"Not a Toolang source project: {source}")
    fingerprint = _source_fingerprint(source)
    destination = cache / fingerprint
    with file_write_lock(cache / f"{fingerprint}.lock"):
        wheels = list(destination.glob("toolang-*.whl"))
        if len(wheels) == 1 and _valid_wheel(wheels[0]):
            _progress(progress, progress_id, "Using cached Toolang wheel", cached=True)
            return wheels[0]
        _progress(progress, progress_id, "Building Toolang wheel...")
        with tempfile.TemporaryDirectory(prefix=".build-", dir=cache) as temporary:
            output = Path(temporary)
            command = _build_command(source, output)
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode:
                detail = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"Could not build Toolang wheel: {detail}")
            built = list(output.glob("toolang-*.whl"))
            if len(built) != 1 or not _valid_wheel(built[0]):
                raise RuntimeError("Toolang build did not produce one valid wheel")
            if _source_fingerprint(source) != fingerprint:
                raise RuntimeError(
                    "Toolang source changed while building; run the command again."
                )
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / built[0].name
            built[0].replace(target)
        _progress(progress, progress_id, "Built Toolang wheel")
        return target


def _source_fingerprint(source: Path) -> str:
    digest = sha256()
    digest.update((repository_source_version(source) or "unknown").encode())
    paths = [source / name for name in _BUILD_FILES]
    paths.extend((source / "src" / "toolang").rglob("*"))
    for path in sorted(paths):
        if (
            not path.is_file()
            or "__pycache__" in path.parts
            or path.suffix in {".pyc", ".pyo"}
        ):
            continue
        digest.update(path.relative_to(source).as_posix().encode())
        digest.update(b"\0")
        digest.update(str(path.stat().st_mode & 0o777).encode())
        digest.update(b"\0")
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _valid_wheel(path: Path) -> bool:
    try:
        with ZipFile(path) as wheel:
            return wheel.testzip() is None and any(
                name.startswith("toolang-") and name.endswith(".dist-info/METADATA")
                for name in wheel.namelist()
            )
    except (BadZipFile, OSError):
        return False


def _build_command(source: Path, output: Path) -> list[str]:
    if uv := shutil.which("uv"):
        return [uv, "build", "--wheel", "--out-dir", str(output), str(source)]
    if find_spec("pip") is not None:
        return [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(output),
            str(source),
        ]
    raise RuntimeError(
        "Building Toolang from source requires uv or pip; install a build tool "
        "or use --dev PATH with a Toolang wheel."
    )


def _progress(
    progress: ProgressSink | None, id: str, label: str, *, cached: bool = False
) -> None:
    emit_progress(
        progress,
        id=id,
        kind="runtime",
        stage="create",
        label=label,
        status="running",
        cached=cached,
    )
