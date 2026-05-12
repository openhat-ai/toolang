"""Prepared lock models and `.prepared` layout helpers."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from .program import PreparedProgram

PreparedVisibility = Literal["shared", "private"]
EntryKind = Literal["psyche", "skill", "service", "prompt", "task", "chore"]
EntryShape = Literal["file", "dir"]
SourceOrigin = Literal["local", "remote", "inline"]
SourceInclusion = Literal["authored", "configured", "referenced", "embedded"]


@dataclass(frozen=True, slots=True)
class PreparedSource:
    """One source record used to rebuild a prepared entry."""

    origin: SourceOrigin
    inclusion: SourceInclusion
    path: str
    updated_at: str
    fingerprint: str
    line: int | None = None

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "origin": self.origin,
            "inclusion": self.inclusion,
            "path": self.path,
            "updated_at": self.updated_at,
            "fingerprint": self.fingerprint,
        }
        if self.line is not None:
            data["line"] = self.line
        return data

    @classmethod
    def from_data(cls, data: dict[str, object]) -> "PreparedSource":
        raw_line = data.get("line")
        line = raw_line if isinstance(raw_line, int) else None
        return cls(
            origin=cast(SourceOrigin, str(data["origin"])),
            inclusion=cast(SourceInclusion, str(data["inclusion"])),
            path=str(data["path"]),
            updated_at=str(data["updated_at"]),
            fingerprint=str(data["fingerprint"]),
            line=line,
        )


@dataclass(frozen=True, slots=True)
class PreparedEntry:
    """One prepared runtime definition."""

    kind: EntryKind
    name: str
    shape: EntryShape
    ref: str
    path: str
    source: PreparedSource
    meta: dict[str, object]

    def to_data(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "shape": self.shape,
            "ref": self.ref,
            "path": self.path,
            "source": self.source.to_data(),
            "meta": dict(self.meta),
        }

    def to_snapshot(self) -> dict[str, object]:
        return self.to_data()

    @classmethod
    def from_data(cls, data: dict[str, object]) -> "PreparedEntry":
        raw_ref = data.get("ref")
        if not isinstance(raw_ref, str) or not raw_ref:
            raw_ref = data.get("locator")
        if not isinstance(raw_ref, str) or not raw_ref:
            raise KeyError("ref")
        return cls(
            kind=cast(EntryKind, str(data["kind"])),
            name=str(data["name"]),
            shape=cast(EntryShape, str(data["shape"])),
            ref=raw_ref,
            path=str(data["path"]),
            source=PreparedSource.from_data(cast(dict[str, object], data["source"])),
            meta=dict(cast(dict[str, object], data.get("meta", {}))),
        )


@dataclass(frozen=True, slots=True)
class PreparedLock:
    """One prepared lock file for one visibility layer."""

    visibility: PreparedVisibility
    updated_at: str
    fingerprint: str
    input_fingerprint: str
    entries: tuple[PreparedEntry, ...]
    program: PreparedProgram | None
    prepared_dir: Path
    lock_path: Path
    lock_mtime_ns: int

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "visibility": self.visibility,
            "updated_at": self.updated_at,
            "fingerprint": self.fingerprint,
            "entries": [entry.to_data() for entry in self.entries],
        }
        data["input_fingerprint"] = self.input_fingerprint
        if self.program is not None:
            data["program"] = self.program.to_data()
        return data

    def to_snapshot(self) -> dict[str, object]:
        data: dict[str, object] = {
            "visibility": self.visibility,
            "updated_at": self.updated_at,
            "fingerprint": self.fingerprint,
            "input_fingerprint": self.input_fingerprint,
            "prepared_dir": str(self.prepared_dir),
            "lock_path": str(self.lock_path),
            "entries": [entry.to_snapshot() for entry in self.entries],
        }
        if self.program is not None:
            data["program"] = self.program.to_snapshot()
        return data


@dataclass(frozen=True, slots=True)
class PreparedState:
    """Combined prepared state for one runtime."""

    toolang_root: Path
    agent_name: str
    shared_lock: PreparedLock
    private_lock: PreparedLock
    program: PreparedProgram
    fingerprint: str
    updated_at: str

    def to_snapshot(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "updated_at": self.updated_at,
            "program": self.program.to_snapshot(),
            "shared": self.shared_lock.to_snapshot(),
            "private": self.private_lock.to_snapshot(),
        }


def shared_prepared_dir(toolang_root: Path) -> Path:
    """Return the shared `.prepared` root."""

    return toolang_root / ".prepared"


def private_prepared_dir(toolang_root: Path, agent_name: str) -> Path:
    """Return the private `.prepared` root for one agent."""

    return toolang_root / "agents" / agent_name / ".prepared"


def shared_lock_path(toolang_root: Path) -> Path:
    """Return the shared lock path."""

    return shared_prepared_dir(toolang_root) / "lock.json"


def private_lock_path(toolang_root: Path, agent_name: str) -> Path:
    """Return the private lock path for one agent."""

    return private_prepared_dir(toolang_root, agent_name) / "lock.json"


def load_shared_lock(toolang_root: Path) -> PreparedLock:
    """Load the shared prepared lock."""

    return _load_lock(shared_lock_path(toolang_root), visibility="shared")


def load_private_lock(toolang_root: Path, agent_name: str) -> PreparedLock:
    """Load the private prepared lock."""

    return _load_lock(private_lock_path(toolang_root, agent_name), visibility="private")


def load_prepared_state(toolang_root: Path, agent_name: str) -> PreparedState:
    """Load both prepared lock files for one runtime."""

    shared_lock = load_shared_lock(toolang_root)
    private_lock = load_private_lock(toolang_root, agent_name)
    return PreparedState(
        toolang_root=toolang_root,
        agent_name=agent_name,
        shared_lock=shared_lock,
        private_lock=private_lock,
        program=_require_program(private_lock),
        fingerprint=_combined_fingerprint(shared_lock.fingerprint, private_lock.fingerprint),
        updated_at=max(shared_lock.updated_at, private_lock.updated_at),
    )


def write_prepared_lock(
    toolang_root: Path,
    lock: PreparedLock,
    *,
    files: dict[str, bytes] | None = None,
) -> PreparedLock:
    """Write one prepared lock and its materialized files."""

    prepared_dir = lock.prepared_dir
    prepared_dir.mkdir(parents=True, exist_ok=True)
    for directory_name in ("inline", "remote"):
        (prepared_dir / directory_name).mkdir(parents=True, exist_ok=True)
    for relative_path, content in sorted((files or {}).items()):
        target = toolang_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        _replace_bytes(target, content)
    _replace_bytes(
        lock.lock_path,
        json.dumps(lock.to_data(), indent=2, sort_keys=True).encode("utf-8"),
    )
    return _load_lock(lock.lock_path, visibility=lock.visibility)


def _load_lock(lock_path: Path, *, visibility: PreparedVisibility) -> PreparedLock:
    data = cast(dict[str, object], json.loads(lock_path.read_text(encoding="utf-8")))
    entries_data = cast(list[dict[str, object]], data.get("entries", []))
    return PreparedLock(
        visibility=visibility,
        updated_at=str(data["updated_at"]),
        fingerprint=str(data["fingerprint"]),
        input_fingerprint=str(data["input_fingerprint"]),
        entries=tuple(PreparedEntry.from_data(item) for item in entries_data),
        program=(
            PreparedProgram.from_data(cast(dict[str, object], data["program"]))
            if isinstance(data.get("program"), dict)
            else None
        ),
        prepared_dir=lock_path.parent,
        lock_path=lock_path,
        lock_mtime_ns=lock_path.stat().st_mtime_ns,
    )


def _combined_fingerprint(shared_fingerprint: str, private_fingerprint: str) -> str:
    digest = sha256()
    digest.update(shared_fingerprint.encode("utf-8"))
    digest.update(b"\0")
    digest.update(private_fingerprint.encode("utf-8"))
    return digest.hexdigest()


def _require_program(lock: PreparedLock) -> PreparedProgram:
    if lock.program is None:
        raise FileNotFoundError("prepared private lock is missing program data")
    return lock.program


def _replace_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    temporary.write_bytes(content)
    os.replace(temporary, path)
