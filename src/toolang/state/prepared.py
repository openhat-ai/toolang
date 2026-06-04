"""Prepared lock models and `.caps` layout helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import frontmatter

from .program import PreparedProgram

PreparedVisibility = Literal["shared", "private"]
EntryKind = Literal["psyche", "skill", "service", "prompt", "task", "chore"]
EntryShape = Literal["file", "dir"]
SourceOrigin = Literal["local", "remote"]
SourceForm = Literal["inline", "ref", "wired", "file"]

_EMPTY_INPUT_FINGERPRINT = sha256().hexdigest()
_EMPTY_LOCK_FINGERPRINT = sha256(b"[]").hexdigest()
_SCHEMA_VERSION = 1
_CAP_DIR_BY_KIND: dict[EntryKind, str] = {
    "psyche": "psyches",
    "skill": "skills",
    "service": "services",
    "prompt": "prompts",
    "task": "tasks",
    "chore": "chores",
}
_KIND_BY_SOURCE_BUCKET: dict[str, EntryKind] = {
    "psyches": "psyche",
    "skills": "skill",
    "services": "service",
    "prompts": "prompt",
    "tasks": "task",
    "chores": "chore",
}
_SOURCE_BUCKET_BY_KIND: dict[EntryKind, str] = {
    kind: bucket for bucket, kind in _KIND_BY_SOURCE_BUCKET.items()
}
_SOURCE_DIRS_SHARED = ("psyches", "skills", "services", "prompts")
_SOURCE_DIRS_PRIVATE = (*_SOURCE_DIRS_SHARED, "tasks", "chores", "drafts", "archive")
_ARTIFACT_BUCKETS = ("inline", "ref", "wired")


@dataclass(frozen=True, slots=True)
class PreparedSource:
    """One source record used to rebuild a prepared entry."""

    origin: SourceOrigin
    form: SourceForm
    path: str
    updated_at: str
    fingerprint: str
    line: int | None = None

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "origin": self.origin,
            "form": self.form,
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
            form=cast(SourceForm, str(data["form"])),
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
    content: str = ""

    def to_data(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "shape": self.shape,
            "ref": self.ref,
            "path": self.path,
            "source": self.source.to_data(),
            "meta": dict(self.meta),
            "content": self.content,
        }

    def to_snapshot(self) -> dict[str, object]:
        return self.to_data()

    @classmethod
    def from_data(cls, data: dict[str, object]) -> "PreparedEntry":
        return cls(
            kind=cast(EntryKind, str(data["kind"])),
            name=str(data["name"]),
            shape=cast(EntryShape, str(data["shape"])),
            ref=str(data["ref"]),
            path=str(data["path"]),
            source=PreparedSource.from_data(cast(dict[str, object], data["source"])),
            meta=dict(cast(dict[str, object], data.get("meta", {}))),
            content=str(data.get("content", "")),
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
        return _lock_manifest(self)

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
    """Return the shared `.caps` root."""

    return toolang_root / ".caps"


def private_prepared_dir(toolang_root: Path, agent_name: str) -> Path:
    """Return the private `.caps` root for one agent."""

    return toolang_root / "agents" / agent_name / ".caps"


def shared_lock_path(toolang_root: Path) -> Path:
    """Return the shared lock path."""

    return shared_prepared_dir(toolang_root) / "lock.json"


def private_lock_path(toolang_root: Path, agent_name: str) -> Path:
    """Return the private lock path for one agent."""

    return private_prepared_dir(toolang_root, agent_name) / "lock.json"


def load_shared_lock(toolang_root: Path) -> PreparedLock:
    """Load the shared prepared lock."""

    lock_path = shared_lock_path(toolang_root)
    if not lock_path.is_file():
        return _empty_shared_lock(toolang_root)
    return _load_lock(lock_path, visibility="shared")


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
    for directory_name in _ARTIFACT_BUCKETS:
        (prepared_dir / directory_name).mkdir(parents=True, exist_ok=True)
    for relative_path, content in sorted((files or {}).items()):
        target = toolang_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        _replace_bytes(target, content)
    _replace_bytes(
        lock.lock_path,
        json.dumps(lock.to_data(), indent=2).encode("utf-8"),
    )
    return _load_lock(lock.lock_path, visibility=lock.visibility)


def _load_lock(lock_path: Path, *, visibility: PreparedVisibility) -> PreparedLock:
    data = cast(dict[str, object], json.loads(lock_path.read_text(encoding="utf-8")))
    if data.get("schema") != _SCHEMA_VERSION:
        raise ValueError(f"unsupported prepared lock schema: {data.get('schema')!r}")
    base = _scope_base(lock_path)
    toolang_root = _toolang_root_from_lock_path(lock_path, visibility=visibility)
    entries = _manifest_entries(data, base=base, toolang_root=toolang_root, visibility=visibility)
    program_data = cast(dict[str, object] | None, cast(dict[str, object], data["prepared"]).get("program"))
    program = _program_from_manifest(program_data, base=base, toolang_root=toolang_root)
    input_fingerprint = _manifest_input_fingerprint(data, base=base, toolang_root=toolang_root, visibility=visibility)
    fingerprint = _manifest_lock_fingerprint(data, entries, base=base, toolang_root=toolang_root)
    if visibility == "private" and program is not None:
        fingerprint = _combined_fingerprint(fingerprint, program.fingerprint())
    return PreparedLock(
        visibility=visibility,
        updated_at=str(data["built_at"]),
        fingerprint=fingerprint,
        input_fingerprint=input_fingerprint,
        entries=entries,
        program=program,
        prepared_dir=lock_path.parent,
        lock_path=lock_path,
        lock_mtime_ns=lock_path.stat().st_mtime_ns,
    )


def _empty_shared_lock(toolang_root: Path) -> PreparedLock:
    lock_path = shared_lock_path(toolang_root)
    return PreparedLock(
        visibility="shared",
        updated_at="",
        fingerprint=_EMPTY_LOCK_FINGERPRINT,
        input_fingerprint=_EMPTY_INPUT_FINGERPRINT,
        entries=(),
        program=None,
        prepared_dir=lock_path.parent,
        lock_path=lock_path,
        lock_mtime_ns=0,
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


def _lock_manifest(lock: PreparedLock) -> dict[str, object]:
    base = _scope_base(lock.lock_path)
    toolang_root = _toolang_root_from_lock_path(lock.lock_path, visibility=lock.visibility)
    sources = _sources_manifest(base=base, visibility=lock.visibility)
    artifacts, artifact_refs = _artifacts_manifest(
        lock.entries,
        base=base,
        toolang_root=toolang_root,
    )
    prepared = _prepared_manifest(
        lock.entries,
        lock.program,
        sources=sources,
        artifact_refs=artifact_refs,
        base=base,
        toolang_root=toolang_root,
    )
    return {
        "schema": _SCHEMA_VERSION,
        "built_at": lock.updated_at,
        "sources": sources,
        "artifacts": artifacts,
        "prepared": prepared,
    }


def _sources_manifest(*, base: Path, visibility: PreparedVisibility) -> dict[str, object]:
    data: dict[str, object] = {}
    if visibility == "private":
        _add_file_source(data, "program", base / "agent.too", base=base)
    _add_file_source(data, "config", base / "config.toml", base=base)
    source_dirs = _SOURCE_DIRS_SHARED if visibility == "shared" else _SOURCE_DIRS_PRIVATE
    for directory_name in source_dirs:
        directory = base / directory_name
        if directory.exists():
            data[directory_name] = _directory_source(directory, base=base, bucket=directory_name)
    return data


def _add_file_source(data: dict[str, object], name: str, path: Path, *, base: Path) -> None:
    if not path.is_file():
        return
    data[name] = _file_manifest(path, base=base)


def _directory_source(path: Path, *, base: Path, bucket: str) -> dict[str, object]:
    items: list[dict[str, object]] = []
    if bucket == "skills":
        for child in sorted(path.iterdir()):
            if child.is_dir():
                items.append(_dir_item_manifest(child, base=base))
            elif child.is_file():
                items.append(_file_manifest(child, base=base))
    else:
        for child in sorted(item for item in path.rglob("*") if item.is_file()):
            items.append(_file_manifest(child, base=base))
    return {
        "path": _scope_relative(path, base=base),
        "mtime": path.stat().st_mtime_ns,
        "items": items,
    }


def _file_manifest(path: Path, *, base: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "path": _scope_relative(path, base=base),
        "shape": "file",
        "mtime": stat.st_mtime_ns,
        "size": stat.st_size,
        "fingerprint": sha256(path.read_bytes()).hexdigest(),
    }


def _dir_item_manifest(path: Path, *, base: Path) -> dict[str, object]:
    return {
        "path": _scope_relative(path, base=base),
        "shape": "dir",
        "mtime": path.stat().st_mtime_ns,
        "items": [
            _file_manifest(child, base=base)
            for child in sorted(item for item in path.rglob("*") if item.is_file())
        ],
    }


def _artifacts_manifest(
    entries: tuple[PreparedEntry, ...],
    *,
    base: Path,
    toolang_root: Path,
) -> tuple[dict[str, object], dict[str, int]]:
    buckets: dict[str, object] = {
        name: _artifact_bucket_manifest(base / ".caps" / name, base=base)
        for name in _ARTIFACT_BUCKETS
    }
    refs: dict[str, int] = {}
    seen_paths: dict[str, set[str]] = {name: set() for name in _ARTIFACT_BUCKETS}
    for entry in entries:
        if entry.source.form == "file":
            continue
        bucket = entry.source.form
        item = _artifact_item_manifest(entry, base=base, toolang_root=toolang_root)
        item_path = str(item["path"])
        if item_path in seen_paths[bucket]:
            bucket_data = cast(dict[str, object], buckets[bucket])
            refs[_entry_key(entry)] = _artifact_item_index(cast(list[dict[str, object]], bucket_data["items"]), item_path)
            continue
        bucket_data = cast(dict[str, object], buckets[bucket])
        items = cast(list[dict[str, object]], bucket_data["items"])
        refs[_entry_key(entry)] = len(items)
        items.append(item)
        seen_paths[bucket].add(item_path)
    return buckets, refs


def _artifact_bucket_manifest(path: Path, *, base: Path) -> dict[str, object]:
    return {
        "path": _scope_relative(path, base=base),
        "mtime": path.stat().st_mtime_ns if path.exists() else 0,
        "items": [],
    }


def _artifact_item_index(items: list[dict[str, object]], path: str) -> int:
    for index, item in enumerate(items):
        if item.get("path") == path:
            return index
    raise KeyError(path)


def _artifact_item_manifest(
    entry: PreparedEntry,
    *,
    base: Path,
    toolang_root: Path,
) -> dict[str, object]:
    entry_path = toolang_root / entry.path
    if entry.shape == "dir":
        root = entry_path.parent
        return _dir_item_manifest(root, base=base)
    return _file_manifest(entry_path, base=base)


def _prepared_manifest(
    entries: tuple[PreparedEntry, ...],
    program: PreparedProgram | None,
    *,
    sources: dict[str, object],
    artifact_refs: dict[str, int],
    base: Path,
    toolang_root: Path,
) -> dict[str, object]:
    caps: list[dict[str, object]] = []
    tasks: list[dict[str, object]] = []
    chores: list[dict[str, object]] = []
    cap_indexes: dict[tuple[str, str], int] = {}
    for entry in entries:
        item = _prepared_item_manifest(
            entry,
            sources=sources,
            artifact_refs=artifact_refs,
            base=base,
            toolang_root=toolang_root,
        )
        if entry.kind in {"task", "chore"}:
            target = tasks if entry.kind == "task" else chores
            target.append(item)
            continue
        cap_indexes[(entry.kind, entry.name)] = len(caps)
        caps.append(item)
    data: dict[str, object] = {
        "caps": caps,
        "tasks": tasks,
        "chores": chores,
    }
    if program is not None:
        program_data = program.to_lock_data()
        _attach_program_prepared_refs(program_data, cap_indexes)
        data["program"] = program_data
    return data


def _prepared_item_manifest(
    entry: PreparedEntry,
    *,
    sources: dict[str, object],
    artifact_refs: dict[str, int],
    base: Path,
    toolang_root: Path,
) -> dict[str, object]:
    item: dict[str, object] = {
        "kind": entry.kind,
        "name": entry.name,
        "form": entry.source.form,
        "source": _prepared_source_ref(entry, sources=sources, base=base, toolang_root=toolang_root),
    }
    origin = _origin_manifest(entry)
    if origin:
        item["origin"] = origin
    artifact = artifact_refs.get(_entry_key(entry))
    if artifact is not None:
        item["artifact"] = artifact
    item["object"] = _object_manifest(entry, toolang_root=toolang_root)
    return item


def _prepared_source_ref(
    entry: PreparedEntry,
    *,
    sources: dict[str, object],
    base: Path,
    toolang_root: Path,
) -> int | str:
    if entry.source.form in {"inline", "ref"}:
        return "program"
    if entry.source.form == "wired":
        return "config"
    bucket_name = _SOURCE_BUCKET_BY_KIND[entry.kind]
    bucket = cast(dict[str, object], sources[bucket_name])
    items = cast(list[dict[str, object]], bucket["items"])
    source_path = _scope_relative(toolang_root / entry.source.path, base=base)
    for index, item in enumerate(items):
        if item.get("path") == source_path:
            return index
    return 0


def _origin_manifest(entry: PreparedEntry) -> dict[str, object]:
    if entry.source.form == "file":
        return {}
    data: dict[str, object] = {}
    if entry.source.line is not None:
        data["line"] = entry.source.line
    if entry.source.form in {"wired", "ref"}:
        data["ref"] = entry.ref
        if entry.ref.startswith("github://"):
            github = _parse_github_ref(entry.ref)
            data.update(github)
    return data


def _parse_github_ref(ref: str) -> dict[str, object]:
    body = ref.removeprefix("github://")
    target, _, rev = body.partition("@")
    parts = target.split("/")
    if len(parts) < 3:
        return {"provider": "github", "commit": rev}
    return {
        "provider": "github",
        "repo": f"{parts[0]}/{parts[1]}",
        "path": "/".join(parts[2:]),
        "commit": rev,
    }


def _object_manifest(entry: PreparedEntry, *, toolang_root: Path) -> dict[str, object]:
    return {
        "meta": dict(entry.meta),
        "content": entry.content or _entry_content(entry, toolang_root=toolang_root),
    }


def _entry_content(entry: PreparedEntry, *, toolang_root: Path) -> str:
    path = toolang_root / entry.path
    if not path.is_file():
        return ""
    return frontmatter.loads(path.read_text(encoding="utf-8")).content.strip()


def _attach_program_prepared_refs(
    program_data: dict[str, object],
    cap_indexes: dict[tuple[str, str], int],
) -> None:
    for item in cast(list[dict[str, object]], program_data.get("caps", [])):
        kind = str(item.get("kind", ""))
        name = str(item.get("name", ""))
        index = cap_indexes.get((kind, name))
        if index is not None:
            item["cap"] = index
    for item in cast(list[dict[str, object]], program_data.get("uses", [])):
        kind = str(item.get("kind", ""))
        ref = str(item.get("ref", ""))
        for (candidate_kind, _), index in cap_indexes.items():
            if candidate_kind == kind:
                item.setdefault("cap", index)
                if item.get("ref") == ref:
                    item["cap"] = index
                    break


def _manifest_entries(
    data: dict[str, object],
    *,
    base: Path,
    toolang_root: Path,
    visibility: PreparedVisibility,
) -> tuple[PreparedEntry, ...]:
    del visibility
    prepared = cast(dict[str, object], data["prepared"])
    entries: list[PreparedEntry] = []
    for item in cast(list[dict[str, object]], prepared.get("caps", [])):
        entries.append(_entry_from_prepared_item(item, data=data, base=base, toolang_root=toolang_root))
    for item in cast(list[dict[str, object]], prepared.get("tasks", [])):
        entries.append(_entry_from_prepared_item(item, data=data, base=base, toolang_root=toolang_root))
    for item in cast(list[dict[str, object]], prepared.get("chores", [])):
        entries.append(_entry_from_prepared_item(item, data=data, base=base, toolang_root=toolang_root))
    return tuple(entries)


def _entry_from_prepared_item(
    item: dict[str, object],
    *,
    data: dict[str, object],
    base: Path,
    toolang_root: Path,
) -> PreparedEntry:
    kind = cast(EntryKind, str(item["kind"]))
    form = cast(SourceForm, str(item["form"]))
    source_item = _manifest_source_item(item, data=data, kind=kind)
    artifact_item = _manifest_artifact_item(item, data=data, form=form)
    source_path = _manifest_entry_source_path(item, source_item=source_item, base=base, toolang_root=toolang_root)
    path, shape = _manifest_entry_path(
        item,
        source_item=source_item,
        artifact_item=artifact_item,
        form=form,
        kind=kind,
        base=base,
        toolang_root=toolang_root,
    )
    object_data = cast(dict[str, object], item.get("object", {}))
    meta = dict(cast(dict[str, object], object_data.get("meta", {})))
    content = object_data.get("content")
    origin = cast(dict[str, object], item.get("origin", {}))
    raw_line = origin.get("line")
    line = raw_line if isinstance(raw_line, int) else None
    return PreparedEntry(
        kind=kind,
        name=str(item["name"]),
        shape=shape,
        ref=_manifest_entry_ref(item, form=form, kind=kind, base=base, toolang_root=toolang_root),
        path=path,
        source=PreparedSource(
            origin="remote" if form in {"ref", "wired"} else "local",
            form=form,
            path=source_path,
            updated_at=_manifest_updated_at(source_item),
            fingerprint=_manifest_item_fingerprint(source_item),
            line=line,
        ),
        meta=meta,
        content=content if isinstance(content, str) else "",
    )


def _manifest_source_item(
    item: dict[str, object],
    *,
    data: dict[str, object],
    kind: EntryKind,
) -> dict[str, object]:
    source = item["source"]
    sources = cast(dict[str, object], data["sources"])
    if isinstance(source, str):
        return cast(dict[str, object], sources[source])
    bucket = cast(dict[str, object], sources[_SOURCE_BUCKET_BY_KIND[kind]])
    return cast(list[dict[str, object]], bucket["items"])[cast(int, source)]


def _manifest_artifact_item(
    item: dict[str, object],
    *,
    data: dict[str, object],
    form: SourceForm,
) -> dict[str, object] | None:
    raw = item.get("artifact")
    if not isinstance(raw, int):
        return None
    artifacts = cast(dict[str, object], data["artifacts"])
    bucket = cast(dict[str, object], artifacts[form])
    return cast(list[dict[str, object]], bucket["items"])[raw]


def _manifest_entry_source_path(
    item: dict[str, object],
    *,
    source_item: dict[str, object],
    base: Path,
    toolang_root: Path,
) -> str:
    source = item["source"]
    if isinstance(source, str):
        return _root_relative(base / str(source_item["path"]), toolang_root=toolang_root)
    return _root_relative(base / str(source_item["path"]), toolang_root=toolang_root)


def _manifest_entry_path(
    item: dict[str, object],
    *,
    source_item: dict[str, object],
    artifact_item: dict[str, object] | None,
    form: SourceForm,
    kind: EntryKind,
    base: Path,
    toolang_root: Path,
) -> tuple[str, EntryShape]:
    if form == "file":
        if kind == "skill" and source_item.get("shape") == "dir":
            return _root_relative(base / str(source_item["path"]) / "SKILL.md", toolang_root=toolang_root), "dir"
        return _root_relative(base / str(source_item["path"]), toolang_root=toolang_root), cast(EntryShape, source_item.get("shape", "file"))
    if artifact_item is None:
        raise KeyError("artifact")
    if artifact_item.get("shape") == "dir":
        entrypoint = "SKILL.md" if kind == "skill" else f"{item['name']}.md"
        return _root_relative(base / str(artifact_item["path"]) / entrypoint, toolang_root=toolang_root), "dir"
    return _root_relative(base / str(artifact_item["path"]), toolang_root=toolang_root), "file"


def _manifest_entry_ref(
    item: dict[str, object],
    *,
    form: SourceForm,
    kind: EntryKind,
    base: Path,
    toolang_root: Path,
) -> str:
    if form in {"ref", "wired"}:
        origin = cast(dict[str, object], item.get("origin", {}))
        return str(origin["ref"])
    if form == "inline":
        return f"inline://{_CAP_DIR_BY_KIND[kind]}/{item['name']}"
    scope = "root" if base == toolang_root else "home"
    return f"{scope}://{_CAP_DIR_BY_KIND[kind]}/{item['name']}"


def _program_from_manifest(
    data: dict[str, object] | None,
    *,
    base: Path,
    toolang_root: Path,
) -> PreparedProgram | None:
    if data is None:
        return None
    source_path = _root_relative(base / "agent.too", toolang_root=toolang_root)
    agent_name = base.name
    return PreparedProgram(
        agent_name=agent_name,
        source_path=source_path,
        source_text=str(data.get("source_text", "")),
        body_text=str(data.get("body_text", "")),
    )


def _manifest_input_fingerprint(
    data: dict[str, object],
    *,
    base: Path,
    toolang_root: Path,
    visibility: PreparedVisibility,
) -> str:
    source_files = _manifest_source_files(cast(dict[str, object], data["sources"]))
    origin = "root" if visibility == "shared" else "agent"
    digest = sha256()
    for item in sorted(source_files, key=lambda value: value["relative_path"]):
        category = _manifest_file_category(str(item["scope_path"]))
        if visibility == "shared" and category == "job":
            continue
        digest.update(_root_relative(base / str(item["scope_path"]), toolang_root=toolang_root).encode("utf-8"))
        digest.update(b"\0")
        digest.update(category.encode("utf-8"))
        digest.update(b"\0")
        digest.update(origin.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["fingerprint"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["size"]).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _manifest_source_files(sources: dict[str, object]) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    for key, value in sources.items():
        item = cast(dict[str, object], value)
        if "fingerprint" in item:
            files.append({"scope_path": item["path"], "relative_path": item["path"], "fingerprint": item["fingerprint"], "size": item["size"]})
            continue
        for child in cast(list[dict[str, object]], item.get("items", [])):
            files.extend(_manifest_item_files(child))
    return files


def _manifest_item_files(item: dict[str, object]) -> list[dict[str, object]]:
    if "fingerprint" in item:
        return [{"scope_path": item["path"], "relative_path": item["path"], "fingerprint": item["fingerprint"], "size": item["size"]}]
    files: list[dict[str, object]] = []
    for child in cast(list[dict[str, object]], item.get("items", [])):
        files.extend(_manifest_item_files(child))
    return files


def _manifest_file_category(scope_path: str) -> str:
    first = Path(scope_path).parts[0]
    if scope_path == "agent.too":
        return "program"
    if scope_path == "config.toml":
        return "config"
    if first in {"tasks", "chores", "drafts", "archive"}:
        return "job"
    return "cap"


def _manifest_lock_fingerprint(
    data: dict[str, object],
    entries: tuple[PreparedEntry, ...],
    *,
    base: Path,
    toolang_root: Path,
) -> str:
    content_fingerprints = _manifest_content_fingerprints(data, base=base, toolang_root=toolang_root)
    payload = [
        {
            "kind": entry.kind,
            "name": entry.name,
            "shape": entry.shape,
            "ref": entry.ref,
            "path": entry.path,
            "source": {
                "origin": entry.source.origin,
                "form": entry.source.form,
                "path": entry.source.path,
                "fingerprint": entry.source.fingerprint,
            },
            "meta": entry.meta,
            "content_fingerprint": content_fingerprints.get(entry.path, entry.source.fingerprint),
        }
        for entry in sorted(entries, key=lambda item: (item.kind, item.name, item.ref))
    ]
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _manifest_content_fingerprints(
    data: dict[str, object],
    *,
    base: Path,
    toolang_root: Path,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for bucket in cast(dict[str, object], data.get("artifacts", {})).values():
        for item in cast(list[dict[str, object]], cast(dict[str, object], bucket).get("items", [])):
            result.update(_manifest_content_fingerprints_for_item(item, base=base, toolang_root=toolang_root))
    return result


def _manifest_content_fingerprints_for_item(
    item: dict[str, object],
    *,
    base: Path,
    toolang_root: Path,
) -> dict[str, str]:
    if "fingerprint" in item:
        return {_root_relative(base / str(item["path"]), toolang_root=toolang_root): str(item["fingerprint"])}
    files = cast(list[dict[str, object]], item.get("items", []))
    digest = sha256()
    for child in sorted(files, key=lambda value: str(value["path"])):
        digest.update(str(Path(str(child["path"])).relative_to(str(item["path"]))).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(child["fingerprint"]).encode("utf-8"))
        digest.update(b"\n")
    entrypoint = base / str(item["path"]) / "SKILL.md"
    return {_root_relative(entrypoint, toolang_root=toolang_root): digest.hexdigest()}


def _manifest_updated_at(item: dict[str, object]) -> str:
    raw_mtime = item.get("mtime", 0)
    mtime = raw_mtime if isinstance(raw_mtime, int) else 0
    return datetime.fromtimestamp(mtime / 1_000_000_000, tz=timezone.utc).isoformat()


def _manifest_item_fingerprint(item: dict[str, object]) -> str:
    if "fingerprint" in item:
        return str(item["fingerprint"])
    digest = sha256()
    for child in sorted(cast(list[dict[str, object]], item.get("items", [])), key=lambda value: str(value["path"])):
        digest.update(str(Path(str(child["path"])).relative_to(str(item["path"]))).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(child["fingerprint"]).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _entry_key(entry: PreparedEntry) -> str:
    return f"{entry.kind}\0{entry.name}\0{entry.ref}"


def _scope_base(lock_path: Path) -> Path:
    return lock_path.parent.parent


def _toolang_root_from_lock_path(lock_path: Path, *, visibility: PreparedVisibility) -> Path:
    if visibility == "shared":
        return lock_path.parent.parent
    return lock_path.parents[3]


def _scope_relative(path: Path, *, base: Path) -> str:
    return str(path.relative_to(base))


def _root_relative(path: Path, *, toolang_root: Path) -> str:
    return str(path.relative_to(toolang_root))
