"""Prepared generation paths and deterministic version calculation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from typing import Literal, Mapping, cast
from uuid import uuid4

from ..common.immutable import freeze_mapping, mutable_data
from ..lang.ast import Program, program_from_data
from .prepared import PreparedEntry
from .source import SourceTree

PreparedScope = Literal["root", "home"]
PREPARED_VERSION_SCHEMA = 1
_PREPARED_VERSION_DOMAIN = b"toolang-prepared-v1\0"
_AGENT_STATE_VERSION_DOMAIN = b"toolang-agent-state-v1\0"
_SOURCE_FILE = "source.json"
_RESOLVED_FILE = "resolved.json"
_PREPARED_FILE = "prepared.json"
_FILES_DIR = "files"
_DOCUMENT_SCHEMA = 1
_INVALID_GENERATION_ERRORS = (OSError, KeyError, TypeError, ValueError)


@dataclass(frozen=True, slots=True)
class RootPrepared:
    """One immutable prepared root generation shared by all agents."""

    version: bytes
    toolang_version: str
    generation_dir: Path
    source: SourceTree
    resolved: Mapping[str, object]
    config: Mapping[str, object]
    caps: tuple[PreparedEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "resolved", freeze_mapping(self.resolved))
        object.__setattr__(self, "config", freeze_mapping(self.config))


@dataclass(frozen=True, slots=True)
class HomePrepared:
    """One immutable prepared generation for an agent home."""

    version: bytes
    toolang_version: str
    generation_dir: Path
    source: SourceTree
    resolved: Mapping[str, object]
    config: Mapping[str, object]
    program: Program
    caps: tuple[PreparedEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "resolved", freeze_mapping(self.resolved))
        object.__setattr__(self, "config", freeze_mapping(self.config))


def prepared_root(toolang_root: Path, agent_name: str | None = None) -> Path:
    """Return the root or agent-home prepared directory."""

    if agent_name is None:
        return toolang_root / ".prepared"
    return toolang_root / "agents" / agent_name / ".prepared"


def prepared_current_path(
    toolang_root: Path, agent_name: str | None = None
) -> Path:
    """Return the current-version pointer path for one prepared scope."""

    return prepared_root(toolang_root, agent_name) / "current"


def prepared_lock_path(toolang_root: Path, agent_name: str | None = None) -> Path:
    """Return the writer lock path for one prepared scope."""

    return prepared_root(toolang_root, agent_name) / "prepare.lock"


def prepared_generation_dir(
    toolang_root: Path,
    version: bytes,
    agent_name: str | None = None,
) -> Path:
    """Return the immutable directory for one prepared version."""

    _require_sha256(version, name="prepared version")
    return prepared_root(toolang_root, agent_name) / "versions" / version.hex()


def load_current_version(
    toolang_root: Path, agent_name: str | None = None
) -> bytes:
    """Load the current prepared version for one scope."""

    value = prepared_current_path(toolang_root, agent_name).read_text(
        encoding="utf-8"
    ).strip()
    try:
        version = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"invalid prepared version: {value!r}") from exc
    _require_sha256(version, name="prepared version")
    return version


def load_generation_source(generation_dir: Path) -> SourceTree:
    """Load source metadata from one immutable generation."""

    return SourceTree.load(generation_dir / _SOURCE_FILE)


def load_generation_resolved(generation_dir: Path) -> dict[str, object]:
    """Load resolved remote-reference data from one generation."""

    return _load_json_object(generation_dir / _RESOLVED_FILE)


def load_generation_prepared(generation_dir: Path) -> dict[str, object]:
    """Load parsed runtime data from one generation."""

    return _load_json_object(generation_dir / _PREPARED_FILE)


def load_root_prepared(
    toolang_root: Path,
    version: bytes | None = None,
) -> RootPrepared:
    """Load and validate one root prepared generation."""

    effective_version = version or load_current_version(toolang_root)
    generation_dir = prepared_generation_dir(toolang_root, effective_version)
    source, resolved, prepared, caps = _load_validated_generation(
        generation_dir,
        version=effective_version,
        scope="root",
    )
    return RootPrepared(
        version=effective_version,
        toolang_version=_document_toolang_version(prepared),
        generation_dir=generation_dir,
        source=source,
        resolved=resolved,
        config=_prepared_config(prepared),
        caps=caps,
    )


def load_home_prepared(
    toolang_root: Path,
    agent_name: str,
    version: bytes | None = None,
) -> HomePrepared:
    """Load and validate one agent-home prepared generation."""

    effective_version = version or load_current_version(toolang_root, agent_name)
    generation_dir = prepared_generation_dir(
        toolang_root, effective_version, agent_name
    )
    source, resolved, prepared, caps = _load_validated_generation(
        generation_dir,
        version=effective_version,
        scope="home",
    )
    return HomePrepared(
        version=effective_version,
        toolang_version=_document_toolang_version(prepared),
        generation_dir=generation_dir,
        source=source,
        resolved=resolved,
        config=_prepared_config(prepared),
        program=_prepared_program(prepared),
        caps=caps,
    )


def write_generation(
    *,
    toolang_root: Path,
    scope: PreparedScope,
    source: SourceTree,
    resolved: Mapping[str, object],
    prepared: Mapping[str, object],
    files: Mapping[str, bytes],
    agent_name: str | None = None,
) -> bytes:
    """Atomically publish one complete, immutable prepared generation."""

    _validate_scope(scope, agent_name=agent_name)
    version = prepared_version(
        scope=scope,
        source=source,
        resolved=resolved,
    )
    target = prepared_generation_dir(toolang_root, version, agent_name)
    versions_dir = target.parent
    versions_dir.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        try:
            _load_validated_generation(target, version=version, scope=scope)
        except _INVALID_GENERATION_ERRORS:
            _quarantine_generation(target)
        else:
            return version
    staging = versions_dir / f".{version.hex()}.tmp-{uuid4().hex}"
    try:
        staging.mkdir()
        source.save(staging / _SOURCE_FILE)
        _write_json(staging / _RESOLVED_FILE, resolved)
        _write_json(staging / _PREPARED_FILE, prepared)
        files_dir = staging / _FILES_DIR
        files_dir.mkdir()
        for relative_path, content in sorted(files.items()):
            relative = _generation_file_path(relative_path)
            destination = files_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        _load_validated_generation(staging, version=version, scope=scope)
        os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return version


def publish_current(
    toolang_root: Path,
    version: bytes,
    agent_name: str | None = None,
) -> None:
    """Atomically point one scope at an existing prepared generation."""

    scope: PreparedScope = "home" if agent_name is not None else "root"
    target = prepared_generation_dir(toolang_root, version, agent_name)
    _load_validated_generation(target, version=version, scope=scope)
    current = prepared_current_path(toolang_root, agent_name)
    current.parent.mkdir(parents=True, exist_ok=True)
    temporary = current.with_name(f".{current.name}.tmp-{uuid4().hex}")
    temporary.write_text(f"{version.hex()}\n", encoding="utf-8")
    os.replace(temporary, current)


@contextmanager
def prepare_lock(
    toolang_root: Path, agent_name: str | None = None
) -> Iterator[None]:
    """Serialize prepared writers for the root or one agent home."""

    path = prepared_lock_path(toolang_root, agent_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def prepared_version(
    *,
    scope: PreparedScope,
    source: SourceTree,
    resolved: Mapping[str, object],
) -> bytes:
    """Return the content-addressed version for one prepared layer."""

    payload = {
        "schema": PREPARED_VERSION_SCHEMA,
        "scope": scope,
        "source": source.to_data(),
        "resolved": mutable_data(resolved),
    }
    digest = sha256()
    digest.update(_PREPARED_VERSION_DOMAIN)
    digest.update(_canonical_json(payload))
    return digest.digest()


def agent_state_version(root_version: bytes, home_version: bytes) -> bytes:
    """Return the version of one exact root and home prepared pair."""

    _require_sha256(root_version, name="root version")
    _require_sha256(home_version, name="home version")
    digest = sha256()
    digest.update(_AGENT_STATE_VERSION_DOMAIN)
    digest.update(root_version)
    digest.update(home_version)
    return digest.digest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"prepared document must be an object: {path}")
    return {str(key): item for key, item in value.items()}


def _load_generation(
    generation_dir: Path,
) -> tuple[SourceTree, dict[str, object], dict[str, object]]:
    _validate_generation_layout(generation_dir)
    return (
        load_generation_source(generation_dir),
        load_generation_resolved(generation_dir),
        load_generation_prepared(generation_dir),
    )


def _load_validated_generation(
    generation_dir: Path,
    *,
    version: bytes,
    scope: PreparedScope,
) -> tuple[
    SourceTree,
    dict[str, object],
    dict[str, object],
    tuple[PreparedEntry, ...],
]:
    source, resolved, prepared = _load_generation(generation_dir)
    _validate_loaded_version(
        version=version,
        scope=scope,
        source=source,
        resolved=resolved,
    )
    _validate_prepared_document(prepared, scope=scope)
    _validate_resolved_document(resolved, generation_dir=generation_dir)
    caps = _prepared_caps(prepared, generation_dir=generation_dir)
    _prepared_config(prepared)
    _document_toolang_version(prepared)
    if scope == "home":
        _prepared_program(prepared)
    return source, resolved, prepared, caps


def _prepared_caps(
    prepared: Mapping[str, object],
    *,
    generation_dir: Path,
) -> tuple[PreparedEntry, ...]:
    raw_caps = prepared.get("caps", [])
    if not isinstance(raw_caps, list):
        raise TypeError("prepared caps must be a list")
    entries: list[PreparedEntry] = []
    for raw in raw_caps:
        if not isinstance(raw, dict):
            raise TypeError("prepared cap must be an object")
        entry = PreparedEntry.from_data(
            {str(key): value for key, value in cast(dict[object, object], raw).items()}
        )
        relative_path = _prepared_file_path(entry.path)
        path = generation_dir / relative_path
        if not path.is_file():
            raise FileNotFoundError(f"prepared cap file not found: {path}")
        entries.append(replace(entry, path=str(path)))
    return tuple(entries)


def _prepared_config(prepared: Mapping[str, object]) -> dict[str, object]:
    raw = prepared.get("config", {})
    if not isinstance(raw, dict):
        raise TypeError("prepared config must be an object")
    return {str(key): value for key, value in raw.items()}


def _prepared_program(prepared: Mapping[str, object]) -> Program:
    if "program" not in prepared:
        raise ValueError("home prepared document is missing program")
    return program_from_data(prepared["program"])


def _validate_loaded_version(
    *,
    version: bytes,
    scope: PreparedScope,
    source: SourceTree,
    resolved: Mapping[str, object],
) -> None:
    expected = prepared_version(
        scope=scope,
        source=source,
        resolved=resolved,
    )
    if expected != version:
        raise ValueError(
            f"prepared generation version mismatch: expected {expected.hex()}, "
            f"found {version.hex()}"
        )


def _document_toolang_version(prepared: Mapping[str, object]) -> str:
    value = prepared.get("toolang_version")
    if not isinstance(value, str) or not value:
        raise ValueError("prepared document is missing toolang_version")
    return value


def _generation_file_path(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"generation file path must be relative: {value!r}")
    return path


def _prepared_file_path(value: str) -> Path:
    path = _generation_file_path(value)
    if path.parts[:1] != (_FILES_DIR,):
        raise ValueError(f"prepared file path must be inside files/: {value!r}")
    return path


def _validate_prepared_document(
    prepared: Mapping[str, object],
    *,
    scope: PreparedScope,
) -> None:
    if prepared.get("schema") != _DOCUMENT_SCHEMA:
        raise ValueError(
            f"unsupported prepared document schema: {prepared.get('schema')!r}"
        )
    if prepared.get("scope") != scope:
        raise ValueError(
            f"prepared document scope mismatch: expected {scope!r}, "
            f"found {prepared.get('scope')!r}"
        )


def _validate_resolved_document(
    resolved: Mapping[str, object],
    *,
    generation_dir: Path,
) -> None:
    if resolved.get("schema") != _DOCUMENT_SCHEMA:
        raise ValueError(
            f"unsupported resolved document schema: {resolved.get('schema')!r}"
        )
    raw_entries = resolved.get("entries")
    if not isinstance(raw_entries, list):
        raise TypeError("resolved entries must be a list")
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise TypeError("resolved entry must be an object")
        entry = cast(dict[object, object], raw_entry)
        raw_files = entry.get("files")
        if not isinstance(raw_files, list):
            raise TypeError("resolved entry files must be a list")
        for raw_file in raw_files:
            if not isinstance(raw_file, dict):
                raise TypeError("resolved file must be an object")
            file = cast(dict[object, object], raw_file)
            relative_path = _prepared_file_path(str(file.get("path", "")))
            path = generation_dir / relative_path
            if not path.is_file():
                raise FileNotFoundError(f"resolved file not found: {path}")
            expected_size = file.get("size")
            if (
                not isinstance(expected_size, int)
                or isinstance(expected_size, bool)
            ):
                raise TypeError("resolved file size must be an integer")
            if path.stat().st_size != expected_size:
                raise ValueError(f"resolved file size mismatch: {path}")


def _validate_generation_layout(path: Path) -> None:
    for name in (_SOURCE_FILE, _RESOLVED_FILE, _PREPARED_FILE):
        if not (path / name).is_file():
            raise FileNotFoundError(f"prepared generation is missing {name}: {path}")
    if not (path / _FILES_DIR).is_dir():
        raise FileNotFoundError(f"prepared generation is missing files: {path}")


def _quarantine_generation(path: Path) -> None:
    quarantine = path.with_name(f".{path.name}.invalid-{uuid4().hex}")
    os.replace(path, quarantine)


def _validate_scope(scope: PreparedScope, *, agent_name: str | None) -> None:
    if scope == "root" and agent_name is not None:
        raise ValueError("root prepared generation cannot have an agent name")
    if scope == "home" and agent_name is None:
        raise ValueError("home prepared generation requires an agent name")


def _require_sha256(value: bytes, *, name: str) -> None:
    if len(value) != sha256().digest_size:
        raise ValueError(f"{name} must contain 32 bytes")
