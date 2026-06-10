"""Caps and local definition helpers."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import tarfile
import tomllib
from typing import Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse, urlsplit
from urllib.request import Request, urlopen

import frontmatter
import tomli_w
from tomli_w._writer import Context as TomlContext
from tomli_w._writer import format_inline_table, format_key_part, format_literal

from .state.durable import DurableFile, DurableState, scan_durable_state
from .common.progress import ProgressSink, emit_progress
from .state.prepared import (
    EntryKind,
    PreparedEntry,
    PreparedLock,
    SourceForm,
    SourceOrigin,
    PreparedVisibility,
    PreparedSource,
    load_private_lock,
    load_shared_lock,
    private_lock_path,
    private_prepared_dir,
    shared_lock_path,
    shared_prepared_dir,
)
from .program import CapDecl
from .state.program import build_prepared_program, load_live_program
from .selectors import Selector, filter_value_matches, parse_selector, split_selector_list, selector_identity_matches

CAP_DIR_NAMES = ("psyches", "skills", "services", "prompts")
JOB_DIR_NAMES = ("chores", "tasks")
CAP_KINDS: tuple[EntryKind, ...] = ("psyche", "skill", "service", "prompt")
JOB_KINDS: tuple[EntryKind, ...] = ("task", "chore")
Visibility = PreparedVisibility
EntryOrigin = SourceOrigin
EntryForm = SourceForm
EntryScope = Literal["root", "home", "here"]
MANAGED_KINDS = frozenset((*CAP_KINDS, *JOB_KINDS))
EMBEDDED_CAP_KINDS = frozenset({"psyche", "service", "prompt"})
FILE_BACKED_KINDS = frozenset({"psyche", "service", "prompt", "task", "chore"})
SKILL_FIELDS = frozenset({"description"})
SERVICE_FIELDS = frozenset({"description", "transport", "target", "headers", "env"})
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DIR_NAME_BY_KIND: dict[EntryKind, str] = {
    "psyche": "psyches",
    "skill": "skills",
    "service": "services",
    "prompt": "prompts",
    "task": "tasks",
    "chore": "chores",
}
KIND_BY_DIR_NAME = {
    "psyches": "psyche",
    "skills": "skill",
    "services": "service",
    "prompts": "prompt",
    "tasks": "task",
    "chores": "chore",
}
CONFIG_SECTION_ORDER = {
    "psyches": 0,
    "skills": 1,
    "services": 2,
    "prompts": 3,
    "tasks": 4,
    "chores": 5,
}
REMOTE_CAP_MATERIALIZE_WORKERS = 4


@dataclass(frozen=True, slots=True)
class _GitHubRemoteRef:
    owner: str
    repo: str
    path: str
    rev: str

    def render(self) -> str:
        return f"github://{self.owner}/{self.repo}/{self.path}@{self.rev}"


@dataclass(frozen=True, slots=True)
class _RemoteEntryRequest:
    visibility: PreparedVisibility
    kind: EntryKind
    ref: str
    name: str | None
    relative_config_path: Path
    config_path: Path
    form: Literal["wired", "ref"]
    source_line: int | None = None


@dataclass(frozen=True, slots=True)
class _CachedRemoteEntry:
    ref: str
    authored_ref: str | None
    source_fingerprint: str
    files: tuple[tuple[str, bytes], ...]


_RemoteEntryCacheKey = tuple[PreparedVisibility, EntryKind, str, str | None, int | None]
_RemoteEntryCache = Mapping[_RemoteEntryCacheKey, _CachedRemoteEntry]


def list_entries(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility | None = None,
    kinds: set[EntryKind] | None = None,
) -> tuple[PreparedEntry, ...]:
    """List local and remote entries from durable authored files and config."""

    durable = scan_durable_state(toolang_root, agent_name)
    entries, _ = _collect_visibility_entries_with_files(durable, visibility=visibility, kinds=kinds)
    return entries


def list_local_entries(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility | None = None,
    kinds: set[EntryKind] | None = None,
) -> tuple[PreparedEntry, ...]:
    """List local cap and job entries from durable authored files."""

    durable = scan_durable_state(toolang_root, agent_name)
    return collect_local_entries(durable, visibility=visibility, kinds=kinds)


def put_local_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    name: str,
    body: str = "",
    meta: Mapping[str, object] | None = None,
) -> Path:
    """Create or replace one local entry file or directory."""

    post = frontmatter.Post(body, **dict(meta or {}))
    return put_local_entry_text(
        toolang_root,
        agent_name,
        visibility=visibility,
        kind=kind,
        name=name,
        text=frontmatter.dumps(post),
    )


def put_local_entry_text(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    name: str,
    text: str,
) -> Path:
    """Create or replace one local entry from raw authored text."""

    _validate_local_kind(visibility, kind)
    _validate_authored_entry_text(kind=kind, text=text)
    entry_path = _local_entry_file_path(toolang_root, agent_name, visibility=visibility, kind=kind, name=name)
    ref = _local_ref(visibility=visibility, kind=kind, name=name)
    _ensure_name_available(toolang_root, agent_name, visibility=visibility, kind=kind, name=name, ref=ref)
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(text, encoding="utf-8")
    return entry_path


def load_local_entry_text(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    name: str,
) -> str:
    """Load one local entry from authored files."""

    _validate_local_kind(visibility, kind)
    entry_path = _local_entry_file_path(toolang_root, agent_name, visibility=visibility, kind=kind, name=name)
    if not entry_path.is_file():
        raise FileNotFoundError(f"local {kind} not found: {name}")
    return entry_path.read_text(encoding="utf-8")


def remove_local_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    name: str,
) -> bool:
    """Remove one local entry if it exists."""

    _validate_local_kind(visibility, kind)
    if kind == "skill":
        target = toolang_root / _relative_definition_root(agent_name, visibility=visibility, kind=kind, name=name)
        if not target.exists():
            return False
        shutil.rmtree(target)
        return True
    entry_path = _local_entry_file_path(toolang_root, agent_name, visibility=visibility, kind=kind, name=name)
    if not entry_path.exists():
        return False
    entry_path.unlink()
    return True


def add_remote_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    ref: str,
    progress: ProgressSink | None = None,
) -> Path:
    """Add one remote entry ref to the authored config file."""

    _validate_local_kind(visibility, kind)
    canonical_ref = _resolve_remote_ref(kind, ref, progress=progress)
    name = _remote_name(kind, canonical_ref)
    _ensure_name_available(
        toolang_root,
        agent_name,
        visibility=visibility,
        kind=kind,
        name=name,
        ref=canonical_ref,
    )
    config_path = _config_path(toolang_root, agent_name, visibility=visibility)
    data = _load_config_data(config_path)
    key = DIR_NAME_BY_KIND[kind]
    table = data.get(key)
    if isinstance(table, dict):
        kind_table = cast(dict[str, object], table)
    else:
        kind_table = {}
        data[key] = kind_table
    kind_table[name] = {"ref": canonical_ref}
    emit_progress(
        progress,
        id=f"cap.config:{kind}:{name}",
        phase="cap.config",
        label=f"Write {kind} config",
        status="running",
        detail=canonical_ref,
    )
    _write_config_data(config_path, data)
    emit_progress(
        progress,
        id=f"cap.config:{kind}:{name}",
        phase="cap.config",
        label=f"Write {kind} config",
        status="ok",
        detail=str(config_path),
    )
    return config_path


def remove_remote_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    name: str,
) -> bool:
    """Remove one remote entry ref by runtime name."""

    _validate_local_kind(visibility, kind)
    return _remove_remote_entries_by_name(
        toolang_root,
        agent_name,
        visibility=visibility,
        kind=kind,
        name=name,
    )


def remote_entry_name(kind: EntryKind, ref: str) -> str:
    """Return the runtime name derived from one remote ref."""

    return _remote_name(kind, _canonicalize_remote_ref(kind, ref))


def entry_visibility(entry: PreparedEntry, *, agent_name: str) -> Visibility:
    """Return the external visibility for one prepared entry."""

    if entry.source.form in {"inline", "ref"}:
        return "private"
    prefix = f"agents/{agent_name}/"
    if entry.path.startswith(prefix) or entry.source.path.startswith(prefix):
        return "private"
    return "shared"


def entry_origin(entry: PreparedEntry) -> EntryOrigin:
    """Return where one prepared entry's content originates."""

    return entry.source.origin


def entry_form(entry: PreparedEntry) -> EntryForm:
    """Return how one prepared entry is authored or attached."""

    return entry.source.form


def entry_scope(entry: PreparedEntry, *, agent_name: str) -> EntryScope:
    """Return where one prepared entry is available."""

    if entry.source.form in {"inline", "ref"}:
        return "here"
    if entry_visibility(entry, agent_name=agent_name) == "shared":
        return "root"
    return "home"


def entry_ref(entry: PreparedEntry, *, agent_name: str) -> str:
    """Return the canonical external ref for one prepared entry."""

    origin = entry_origin(entry)
    if origin == "remote":
        return entry.ref
    if entry.source.form == "inline":
        return f"inline://{DIR_NAME_BY_KIND[entry.kind]}/{entry.name}"
    visibility = entry_visibility(entry, agent_name=agent_name)
    return f"{'root' if visibility == 'shared' else 'home'}://{DIR_NAME_BY_KIND[entry.kind]}/{entry.name}"


def entry_definition_file(entry: PreparedEntry) -> str:
    """Return the authored file that defines or links one prepared entry."""

    if entry.source.form == "file":
        return entry.path
    return entry.source.path


def entry_line(entry: PreparedEntry) -> int | None:
    """Return the authored source line for one prepared entry when known."""

    return entry.source.line


def split_cap_selectors(items: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Split repeated and CSV cap selector inputs."""

    return split_selector_list(items)


def cap_entry_matches_selector(
    entry: PreparedEntry,
    selector: str | Selector,
    *,
    agent_name: str,
    implicit_kind: EntryKind | None = None,
) -> bool:
    """Return whether one cap entry matches a cap selector."""

    parsed = (
        selector
        if isinstance(selector, Selector)
        else parse_selector(selector, domain="cap", implicit_family=implicit_kind)
    )
    if implicit_kind is not None and entry.kind != implicit_kind:
        return False
    if not selector_identity_matches(family=entry.kind, name=entry.name, selector=parsed):
        return False
    for key, values in parsed.filters.items():
        actual = _entry_selector_filter_value(entry, key, agent_name=agent_name)
        if actual is None or not filter_value_matches(actual, values):
            return False
    return True


def select_cap_entries(
    entries: tuple[PreparedEntry, ...],
    selectors: list[str] | tuple[str, ...] | None,
    *,
    agent_name: str,
    implicit_kind: EntryKind | None = None,
) -> tuple[PreparedEntry, ...]:
    """Return entries selected by a selector list."""

    parsed = tuple(
        parse_selector(raw, domain="cap", implicit_family=implicit_kind)
        for raw in split_cap_selectors(selectors)
    )
    if not parsed:
        return entries
    selected: list[PreparedEntry] = []
    seen: set[tuple[str, str, str]] = set()
    for selector in parsed:
        for entry in entries:
            identity = (entry.kind, entry.name, entry.ref)
            if identity in seen:
                continue
            if cap_entry_matches_selector(
                entry,
                selector,
                agent_name=agent_name,
                implicit_kind=implicit_kind,
            ):
                selected.append(entry)
                seen.add(identity)
    return tuple(selected)


def _entry_selector_filter_value(
    entry: PreparedEntry,
    key: str,
    *,
    agent_name: str,
) -> str | None:
    if key == "scope":
        return entry_scope(entry, agent_name=agent_name)
    if key == "form":
        return entry_form(entry)
    if key == "origin":
        return entry_origin(entry)
    return None


def collect_local_entries(
    durable: DurableState,
    *,
    visibility: PreparedVisibility | None = None,
    kinds: set[EntryKind] | None = None,
) -> tuple[PreparedEntry, ...]:
    """Collect local prepared entries from durable authored files."""

    entries: dict[str, PreparedEntry] = {}
    for item in durable.files:
        entry = _local_entry_from_file(durable.toolang_root, durable.agent_name, item)
        if entry is None:
            continue
        entry_visibility_value: PreparedVisibility = "shared" if item.origin == "root" else "private"
        if visibility is not None and entry_visibility_value != visibility:
            continue
        if kinds is not None and entry.kind not in kinds:
            continue
        entries.setdefault(entry.ref, entry)
    return tuple(sorted(entries.values(), key=_entry_sort_key))


def _collect_visibility_entries_with_files(
    durable: DurableState,
    *,
    visibility: PreparedVisibility | None = None,
    kinds: set[EntryKind] | None = None,
    materialize_remote: bool = False,
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
) -> tuple[tuple[PreparedEntry, ...], dict[str, bytes]]:
    effective_remote_cache = remote_cache
    if effective_remote_cache is None and not materialize_remote:
        effective_remote_cache = _existing_remote_cache(durable, visibility=visibility)
    local_entries = collect_local_entries(durable, visibility=visibility, kinds=kinds)
    remote_entries, files = _collect_remote_entries(
        durable.toolang_root,
        durable.agent_name,
        visibility=visibility,
        kinds=kinds,
        materialize=materialize_remote,
        remote_cache=effective_remote_cache,
        progress=progress,
    )
    embedded_entries, embedded_files = _collect_program_embedded_entries(
        durable,
        visibility=visibility,
        kinds=kinds,
        materialize=materialize_remote,
    )
    use_entries, use_files = _collect_program_use_entries(
        durable,
        visibility=visibility,
        kinds=kinds,
        materialize=materialize_remote,
        remote_cache=effective_remote_cache,
        progress=progress,
    )
    files.update(embedded_files)
    files.update(use_files)
    entries = _dedupe_entries((*local_entries, *remote_entries, *embedded_entries, *use_entries))
    return entries, files


def _existing_remote_cache(
    durable: DurableState,
    *,
    visibility: PreparedVisibility | None,
) -> _RemoteEntryCache | None:
    cache: dict[_RemoteEntryCacheKey, _CachedRemoteEntry] = {}
    visibilities = ("shared", "private") if visibility is None else (visibility,)
    for item_visibility in visibilities:
        try:
            lock = (
                load_shared_lock(durable.toolang_root)
                if item_visibility == "shared"
                else load_private_lock(durable.toolang_root, durable.agent_name)
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError):
            continue
        cache.update(remote_entry_cache(durable.toolang_root, lock))
    return cache or None


def build_visibility_lock(
    durable: DurableState,
    *,
    visibility: PreparedVisibility,
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
) -> tuple[PreparedLock, dict[str, bytes]]:
    """Build one prepared lock and any materialized files for one visibility."""

    emit_progress(
        progress,
        id=f"prepare.visibility:{visibility}",
        phase="prepare.visibility",
        label=f"Prepare {visibility} caps",
        status="running",
        detail=durable.agent_name,
    )
    entries, files = _collect_visibility_entries_with_files(
        durable,
        visibility=visibility,
        materialize_remote=True,
        remote_cache=remote_cache,
        progress=progress,
    )
    _ensure_no_conflicts(entries)
    updated_at = datetime.now(timezone.utc).isoformat()
    if visibility == "shared":
        prepared_dir = shared_prepared_dir(durable.toolang_root)
        lock_path = shared_lock_path(durable.toolang_root)
    else:
        prepared_dir = private_prepared_dir(durable.toolang_root, durable.agent_name)
        lock_path = private_lock_path(durable.toolang_root, durable.agent_name)
    result = (
        PreparedLock(
            visibility=visibility,
            updated_at=updated_at,
            fingerprint=_lock_fingerprint(durable.toolang_root, entries, files),
            input_fingerprint=visibility_input_fingerprint(durable, visibility=visibility),
            entries=entries,
            program=None,
            prepared_dir=prepared_dir,
            lock_path=lock_path,
            lock_mtime_ns=0,
        ),
        files,
    )
    emit_progress(
        progress,
        id=f"prepare.visibility:{visibility}",
        phase="prepare.visibility",
        label=f"Prepare {visibility} caps",
        status="ok",
        detail=f"{len(entries)} entries",
    )
    return result


def remote_entry_cache(
    toolang_root: Path,
    lock: PreparedLock,
) -> dict[_RemoteEntryCacheKey, _CachedRemoteEntry]:
    """Return reusable remote artifacts from one existing prepared lock."""

    cache: dict[_RemoteEntryCacheKey, _CachedRemoteEntry] = {}
    manifest = _load_lock_manifest_optional(lock)
    for entry in lock.entries:
        if entry.source.origin != "remote":
            continue
        if manifest is not None and not _entry_artifact_matches_manifest(toolang_root, entry, manifest):
            continue
        key = _remote_entry_cache_key(
            visibility=lock.visibility,
            kind=entry.kind,
            form=entry.source.form,
            name=entry.name if entry.source.form == "wired" else None,
            source_line=entry.source.line,
        )
        files = _cache_entry_files(toolang_root, entry)
        if files is None:
            continue
        cache[key] = _CachedRemoteEntry(
            ref=entry.ref,
            authored_ref=_entry_authored_ref(manifest, entry) if manifest is not None else None,
            source_fingerprint=entry.source.fingerprint,
            files=files,
        )
    return cache


def _load_lock_manifest_optional(lock: PreparedLock) -> dict[str, object] | None:
    try:
        return cast(dict[str, object], json.loads(lock.lock_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def _entry_artifact_matches_manifest(
    toolang_root: Path,
    entry: PreparedEntry,
    manifest: dict[str, object],
) -> bool:
    item = _entry_artifact_manifest(entry, manifest)
    if item is None:
        return False
    entry_path = toolang_root / entry.path
    if entry.shape == "file":
        return entry_path.is_file() and item.get("fingerprint") == _file_fingerprint(entry_path)
    if not entry_path.parent.is_dir():
        return False
    expected = _artifact_child_fingerprints(item)
    actual = {
        str(path.relative_to(entry_path.parent)): _file_fingerprint(path)
        for path in sorted(child for child in entry_path.parent.rglob("*") if child.is_file())
    }
    return actual == expected


def _entry_authored_ref(
    manifest: dict[str, object],
    entry: PreparedEntry,
) -> str | None:
    if entry.source.form != "ref" or entry.source.line is None:
        return None
    prepared = cast(dict[str, object], manifest.get("prepared", {}))
    program = cast(dict[str, object], prepared.get("program", {}))
    for item in cast(list[dict[str, object]], program.get("uses", [])):
        if item.get("kind") == entry.kind and item.get("line") == entry.source.line:
            ref = item.get("ref")
            return ref if isinstance(ref, str) else None
    return None


def _entry_artifact_manifest(
    entry: PreparedEntry,
    manifest: dict[str, object],
) -> dict[str, object] | None:
    prepared = cast(dict[str, object], manifest.get("prepared", {}))
    for collection_name in ("caps", "tasks", "chores"):
        for item in cast(list[dict[str, object]], prepared.get(collection_name, [])):
            if item.get("kind") != entry.kind or item.get("name") != entry.name:
                continue
            origin = item.get("origin")
            if isinstance(origin, dict) and cast(dict[str, object], origin).get("ref") != entry.ref:
                continue
            artifact_index = item.get("artifact")
            if not isinstance(artifact_index, int):
                return None
            bucket = cast(dict[str, object], cast(dict[str, object], manifest.get("artifacts", {})).get(entry.source.form, {}))
            artifacts = cast(list[dict[str, object]], bucket.get("items", []))
            if artifact_index >= len(artifacts):
                return None
            return artifacts[artifact_index]
    return None


def _artifact_child_fingerprints(item: dict[str, object]) -> dict[str, str]:
    root = Path(str(item.get("path", "")))
    result: dict[str, str] = {}
    for child in cast(list[dict[str, object]], item.get("items", [])):
        child_path = Path(str(child.get("path", "")))
        try:
            relative_path = child_path.relative_to(root)
        except ValueError:
            return {}
        result[str(relative_path)] = str(child.get("fingerprint", ""))
    return result


def _remote_entry_cache_key(
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    form: SourceForm,
    name: str | None,
    source_line: int | None,
) -> _RemoteEntryCacheKey:
    return (visibility, kind, form, name if form == "wired" else None, source_line)


def _cache_entry_files(
    toolang_root: Path,
    entry: PreparedEntry,
) -> tuple[tuple[str, bytes], ...] | None:
    entry_path = toolang_root / entry.path
    if entry.shape == "dir":
        root = entry_path.parent
        if not root.is_dir():
            return None
        return tuple(
            (str(path.relative_to(root)), path.read_bytes())
            for path in sorted(item for item in root.rglob("*") if item.is_file())
        )
    if not entry_path.is_file():
        return None
    return (("", entry_path.read_bytes()),)


def _cached_remote_entry(
    remote_cache: _RemoteEntryCache | None,
    request: _RemoteEntryRequest,
) -> _CachedRemoteEntry | None:
    if remote_cache is None:
        return None
    key = _remote_entry_cache_key(
        visibility=request.visibility,
        kind=request.kind,
        form=request.form,
        name=request.name,
        source_line=request.source_line,
    )
    cached = remote_cache.get(key)
    if cached is None:
        return None
    if "://" in request.ref and _canonicalize_remote_ref(request.kind, request.ref) == cached.ref:
        return cached
    if request.form == "ref" and request.ref in {cached.ref, cached.authored_ref}:
        return cached
    if cached.source_fingerprint != _file_fingerprint(request.config_path):
        return None
    return cached


def _remap_cached_remote_files(
    cached: _CachedRemoteEntry,
    relative_entry_path: Path,
) -> dict[str, bytes]:
    if len(cached.files) == 1 and cached.files[0][0] == "":
        return {str(relative_entry_path): cached.files[0][1]}
    root = relative_entry_path.parent
    return {str(root / relative_path): content for relative_path, content in cached.files}


def _emit_cached_remote_progress(
    request: _RemoteEntryRequest,
    canonical_ref: str,
    *,
    progress: ProgressSink | None,
) -> None:
    text = request.ref.strip()
    if "://" not in text:
        emit_progress(
            progress,
            id=f"cap.resolve:{request.kind}:{text}",
            phase="cap.resolve",
            label=f"Resolve {request.kind}",
            status="ok",
            detail=canonical_ref,
        )
        return
    emit_progress(
        progress,
        id=f"cap.fetch:{request.kind}:{canonical_ref}",
        phase="cap.fetch",
        label=f"Fetch {request.kind}",
        status="ok",
        detail="cached",
    )


def visibility_input_fingerprint(
    durable: DurableState,
    *,
    visibility: PreparedVisibility,
) -> str:
    """Return the authored input fingerprint for one visibility lock."""

    return _durable_files_fingerprint(_visibility_input_files(durable, visibility=visibility))


def visibility_lock_content_fingerprint(toolang_root: Path, lock: PreparedLock) -> str:
    """Recompute one visibility lock fingerprint from current local content."""

    return _lock_fingerprint(
        toolang_root,
        lock.entries,
        _prepared_materialized_files(toolang_root, lock.entries),
    )


def effective_cap_entries(
    shared_lock: PreparedLock,
    private_lock: PreparedLock,
) -> tuple[PreparedEntry, ...]:
    """Return the runtime-visible cap set after visibility precedence is applied."""

    effective: dict[tuple[str, str], PreparedEntry] = {}
    for entry in shared_lock.entries:
        if entry.kind in CAP_KINDS:
            effective[(entry.kind, entry.name)] = entry
    for entry in private_lock.entries:
        if entry.kind in CAP_KINDS:
            effective[(entry.kind, entry.name)] = entry
    return tuple(sorted(effective.values(), key=_entry_sort_key))


def active_job_entries(private_lock: PreparedLock) -> tuple[PreparedEntry, ...]:
    """Return active runtime job entries."""

    jobs = [
        entry
        for entry in private_lock.entries
        if entry.kind in JOB_KINDS
        and len(Path(entry.path).parts) >= 2
        and Path(entry.path).parts[-2] in {"tasks", "chores"}
    ]
    return tuple(sorted(jobs, key=_entry_sort_key))


def durable_entries_snapshot(
    durable: DurableState,
) -> dict[str, object]:
    """Return a JSON-friendly durable definitions snapshot."""

    shared_entries, _ = _collect_visibility_entries_with_files(durable, visibility="shared")
    private_entries, _ = _collect_visibility_entries_with_files(durable, visibility="private")
    return {
        "program_source": durable.program_source,
        "config_paths": list(durable.config_paths),
        "shared_entries": [entry.to_snapshot() for entry in shared_entries],
        "private_entries": [entry.to_snapshot() for entry in private_entries],
    }


def _local_entry_from_file(
    toolang_root: Path,
    agent_name: str,
    item: DurableFile,
) -> PreparedEntry | None:
    if item.category not in {"cap", "job"}:
        return None
    visibility: PreparedVisibility = "shared" if item.origin == "root" else "private"
    relative_path = Path(item.relative_path)
    local_parts = _local_parts(relative_path, agent_name=agent_name, visibility=visibility)
    if len(local_parts) < 2:
        return None
    directory_name = local_parts[0]
    kind = cast(EntryKind | None, KIND_BY_DIR_NAME.get(directory_name))
    if kind is None:
        return None
    if kind == "skill":
        return _skill_entry(toolang_root, agent_name, visibility=visibility, name=local_parts[1])
    if kind in FILE_BACKED_KINDS and len(local_parts) == 2:
        return _file_entry(toolang_root, relative_path, kind=kind)
    return None


def _skill_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    name: str,
) -> PreparedEntry | None:
    root_relative_dir = _relative_definition_root(agent_name, visibility=visibility, kind="skill", name=name)
    root_relative_file = root_relative_dir / "SKILL.md"
    entry_file = toolang_root / root_relative_file
    if not entry_file.is_file():
        return None
    source_path = toolang_root / root_relative_dir
    return PreparedEntry(
        kind="skill",
        name=name,
        shape="dir",
        ref=_local_ref(visibility=visibility, kind="skill", name=name),
        path=str(root_relative_file),
        source=_source_record(
            root_relative_path=root_relative_dir,
            absolute_path=source_path,
            origin="local",
                form="file",
            shape="dir",
        ),
        meta=_load_meta(entry_file),
    )


def _file_entry(
    toolang_root: Path,
    relative_path: Path,
    *,
    kind: EntryKind,
) -> PreparedEntry | None:
    absolute_path = toolang_root / relative_path
    if not absolute_path.is_file():
        return None
    return PreparedEntry(
        kind=kind,
        name=relative_path.stem,
        shape="file",
        ref=_local_ref(visibility=_visibility_from_relative_path(relative_path), kind=kind, name=relative_path.stem),
        path=str(relative_path),
        source=_source_record(
            root_relative_path=relative_path,
            absolute_path=absolute_path,
            origin="local",
            form="file",
            shape="file",
        ),
        meta=_load_meta(absolute_path),
    )


def _source_record(
    *,
    root_relative_path: Path,
    absolute_path: Path,
    origin: EntryOrigin,
    form: EntryForm,
    shape: Literal["file", "dir"],
    line: int | None = None,
) -> PreparedSource:
    fingerprint = _dir_fingerprint(absolute_path) if shape == "dir" else _file_fingerprint(absolute_path)
    return PreparedSource(
        origin=origin,
        form=form,
        path=str(root_relative_path),
        updated_at=_updated_at(absolute_path, shape=shape),
        fingerprint=fingerprint,
        line=line,
    )


def _file_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ensure_no_conflicts(entries: tuple[PreparedEntry, ...]) -> None:
    seen: dict[tuple[str, str], str] = {}
    for entry in entries:
        key = (entry.kind, entry.name)
        existing = seen.get(key)
        if existing is not None and existing != entry.ref:
            raise ValueError(
                f"conflicting entries in one visibility: kind={entry.kind} name={entry.name}"
            )
        seen[key] = entry.ref


def _dedupe_entries(entries: tuple[PreparedEntry, ...]) -> tuple[PreparedEntry, ...]:
    by_ref: dict[str, PreparedEntry] = {}
    for entry in sorted(entries, key=_entry_sort_key):
        by_ref.setdefault(entry.ref, entry)
    return tuple(sorted(by_ref.values(), key=_entry_sort_key))


def _visibility_input_files(
    durable: DurableState,
    *,
    visibility: PreparedVisibility,
) -> tuple[DurableFile, ...]:
    if visibility == "shared":
        return tuple(
            item
            for item in durable.files
            if item.origin == "root" and item.category in {"config", "cap"}
        )
    return tuple(
        item
        for item in durable.files
        if item.origin == "agent" and item.category in {"config", "cap", "job", "program"}
    )


def _durable_files_fingerprint(files: tuple[DurableFile, ...]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(item.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.category.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.origin.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.digest.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item.size).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _lock_fingerprint(
    toolang_root: Path,
    entries: tuple[PreparedEntry, ...],
    materialized_files: Mapping[str, bytes],
) -> str:
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
            "content_fingerprint": _content_fingerprint(toolang_root, entry, materialized_files),
        }
        for entry in sorted(entries, key=_entry_sort_key)
    ]
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prepared_materialized_files(
    toolang_root: Path,
    entries: tuple[PreparedEntry, ...],
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for entry in entries:
        if entry.source.form == "file":
            continue
        entry_path = toolang_root / entry.path
        if entry.shape == "dir":
            root = entry_path.parent
            if not root.is_dir():
                raise FileNotFoundError(f"prepared entry directory not found: {root}")
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                files[str(path.relative_to(toolang_root))] = path.read_bytes()
            continue
        if not entry_path.is_file():
            raise FileNotFoundError(f"prepared entry file not found: {entry_path}")
        files[entry.path] = entry_path.read_bytes()
    return files


def _content_fingerprint(
    toolang_root: Path,
    entry: PreparedEntry,
    materialized_files: Mapping[str, bytes],
) -> str:
    if entry.source.form == "file":
        entry_path = toolang_root / entry.path
        if entry.shape == "dir":
            return _dir_fingerprint(entry_path.parent)
        return hashlib.sha256(entry_path.read_bytes()).hexdigest()
    if entry.shape == "dir":
        return _materialized_dir_fingerprint(Path(entry.path).parent, materialized_files)
    return hashlib.sha256(materialized_files[entry.path]).hexdigest()


def _dir_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(item for item in path.rglob("*") if item.is_file())
    for item in files:
        digest.update(str(item.relative_to(path)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(item.read_bytes()).hexdigest().encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _materialized_dir_fingerprint(
    root_relative_dir: Path,
    files: Mapping[str, bytes],
) -> str:
    digest = hashlib.sha256()
    selected = sorted(
        (Path(path), content)
        for path, content in files.items()
        if Path(path).is_relative_to(root_relative_dir)
    )
    for path, content in selected:
        digest.update(str(path.relative_to(root_relative_dir)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).hexdigest().encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _updated_at(path: Path, *, shape: Literal["file", "dir"]) -> str:
    if shape == "file":
        return datetime.fromtimestamp(path.stat().st_mtime_ns / 1_000_000_000, tz=timezone.utc).isoformat()
    timestamps = [item.stat().st_mtime_ns for item in path.rglob("*") if item.is_file()]
    timestamps.append(path.stat().st_mtime_ns)
    latest = max(timestamps)
    return datetime.fromtimestamp(latest / 1_000_000_000, tz=timezone.utc).isoformat()


def _load_meta(path: Path) -> dict[str, object]:
    post = frontmatter.loads(path.read_text(encoding="utf-8"))
    return cast(dict[str, object], _json_compatible(dict(post.metadata)))


def _load_meta_text(text: str) -> dict[str, object]:
    post = frontmatter.loads(text)
    return cast(dict[str, object], _json_compatible(dict(post.metadata)))


def _json_compatible(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return [_json_compatible(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def _entry_sort_key(entry: PreparedEntry) -> tuple[str, str, str]:
    return (entry.kind, entry.name, entry.ref)


def _validate_local_kind(visibility: PreparedVisibility, kind: EntryKind) -> None:
    if kind not in MANAGED_KINDS:
        raise ValueError(f"unsupported kind: {kind}")
    if visibility == "shared" and kind in JOB_KINDS:
        raise ValueError(f"shared visibility does not support kind: {kind}")


def _validate_authored_entry_text(*, kind: EntryKind, text: str) -> None:
    if kind not in {"skill", "service"}:
        return
    post = frontmatter.loads(text)
    meta = dict(post.metadata)
    if kind == "skill":
        _require_exact_meta_fields(kind=kind, meta=meta, allowed=SKILL_FIELDS)
        description = meta.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("skill description is required")
        if not post.content.strip():
            raise ValueError("skill body is required")
        return

    _require_exact_meta_fields(kind=kind, meta=meta, allowed=SERVICE_FIELDS)
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("service description is required")
    transport = meta.get("transport")
    if transport not in {"http", "stdio"}:
        raise ValueError("service transport must be http or stdio")
    target = meta.get("target")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("service target is required")
    headers = meta.get("headers")
    if headers is not None and not _is_string_map(headers):
        raise ValueError("service headers must be a string map")
    env = meta.get("env")
    if env is not None and not _is_env_names(env):
        raise ValueError("service env must list environment variable names")


def _require_exact_meta_fields(
    *,
    kind: EntryKind,
    meta: Mapping[str, object],
    allowed: frozenset[str],
) -> None:
    unknown = sorted(set(meta) - set(allowed))
    if unknown:
        joined = ", ".join(repr(item) for item in unknown)
        raise ValueError(f"{kind} has unsupported frontmatter fields: {joined}")


def _is_string_map(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    )


def _is_env_names(value: object) -> bool:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list | tuple):
        items = [item.strip() for item in value if isinstance(item, str)]
        if len(items) != len(value):
            return False
    else:
        return False
    return bool(items) and all(ENV_NAME_RE.fullmatch(item) is not None for item in items)


def _ensure_name_available(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    name: str,
    ref: str,
) -> None:
    for existing_name, existing_ref in _existing_name_refs(
        toolang_root,
        agent_name,
        visibility=visibility,
        kind=kind,
    ):
        if existing_name == name and existing_ref != ref:
            raise ValueError(f"conflicting entries in one visibility: kind={kind} name={name}")


def _existing_name_refs(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
) -> tuple[tuple[str, str], ...]:
    durable = scan_durable_state(toolang_root, agent_name)
    items = [
        (entry.name, entry.ref)
        for entry in collect_local_entries(durable, visibility=visibility, kinds={kind})
    ]
    items.extend(_configured_remote_name_refs(toolang_root, agent_name, visibility=visibility, kind=kind))
    items.extend(_prepared_name_refs(toolang_root, agent_name, visibility=visibility, kind=kind))
    return tuple(dict.fromkeys(items))


def _configured_remote_name_refs(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
) -> tuple[tuple[str, str], ...]:
    if kind not in CAP_KINDS:
        return ()
    data = _load_config_data(_config_path(toolang_root, agent_name, visibility=visibility))
    table = _config_kind_table_optional(data, kind)
    if table is None:
        return ()
    return tuple((name, _config_ref(item)) for name, item in sorted(table.items()))


def _prepared_name_refs(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
) -> tuple[tuple[str, str], ...]:
    try:
        lock = (
            load_shared_lock(toolang_root)
            if visibility == "shared"
            else load_private_lock(toolang_root, agent_name)
        )
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return ()
    return tuple((entry.name, entry.ref) for entry in lock.entries if entry.kind == kind)


def _local_entry_file_path(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    name: str,
) -> Path:
    return toolang_root / _relative_entry_file_path(agent_name, visibility=visibility, kind=kind, name=name)


def _relative_entry_file_path(
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    name: str,
) -> Path:
    definition_root = _relative_definition_root(agent_name, visibility=visibility, kind=kind, name=name)
    if kind == "skill":
        return definition_root / "SKILL.md"
    return definition_root.with_suffix(".md")


def _relative_definition_root(
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    name: str,
) -> Path:
    prefix = Path() if visibility == "shared" else Path("agents") / agent_name
    return prefix / DIR_NAME_BY_KIND[kind] / name


def _local_ref(*, visibility: PreparedVisibility, kind: EntryKind, name: str) -> str:
    scheme = "root" if visibility == "shared" else "home"
    return f"{scheme}://{DIR_NAME_BY_KIND[kind]}/{name}"


def _visibility_from_relative_path(relative_path: Path) -> PreparedVisibility:
    return "private" if relative_path.parts[:1] == ("agents",) else "shared"


def _local_parts(relative_path: Path, *, agent_name: str, visibility: PreparedVisibility) -> tuple[str, ...]:
    if visibility == "private" and relative_path.parts[:2] == ("agents", agent_name):
        return relative_path.parts[2:]
    return relative_path.parts


def _collect_remote_entries(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility | None = None,
    kinds: set[EntryKind] | None = None,
    materialize: bool = False,
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
) -> tuple[tuple[PreparedEntry, ...], dict[str, bytes]]:
    requests = _collect_remote_entry_requests(
        toolang_root,
        agent_name,
        visibility=visibility,
        kinds=kinds,
    )
    return _materialize_remote_entry_requests(
        toolang_root,
        agent_name,
        requests,
        materialize=materialize,
        remote_cache=remote_cache,
        progress=progress,
    )


def _collect_remote_entry_requests(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility | None,
    kinds: set[EntryKind] | None,
) -> tuple[_RemoteEntryRequest, ...]:
    visibilities = ("shared", "private") if visibility is None else (visibility,)
    requests: list[_RemoteEntryRequest] = []
    for item_visibility in visibilities:
        config_path = _config_path(toolang_root, agent_name, visibility=item_visibility)
        if not config_path.is_file():
            continue
        data = _load_config_data(config_path)
        relative_config_path = config_path.relative_to(toolang_root)
        for kind_name in DIR_NAME_BY_KIND:
            if kind_name in JOB_KINDS:
                continue
            kind = cast(EntryKind, kind_name)
            if kinds is not None and kind not in kinds:
                continue
            kind_table = _config_kind_table_optional(data, kind)
            if kind_table is None:
                continue
            for name, item in sorted(kind_table.items()):
                requests.append(
                    _RemoteEntryRequest(
                        visibility=item_visibility,
                        kind=kind,
                        ref=_config_ref(item),
                        name=name,
                        relative_config_path=relative_config_path,
                        config_path=config_path,
                        form="wired",
                    )
                )
    return tuple(requests)


def _collect_program_use_entries(
    durable: DurableState,
    *,
    visibility: PreparedVisibility | None = None,
    kinds: set[EntryKind] | None = None,
    materialize: bool = False,
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
) -> tuple[tuple[PreparedEntry, ...], dict[str, bytes]]:
    if visibility == "shared" or durable.program_source is None:
        return (), {}
    prepared_program = build_prepared_program(durable)
    live_program = load_live_program(prepared_program)
    relative_program_path = Path(prepared_program.source_path)
    program_path = durable.toolang_root / relative_program_path
    line_offset = _program_body_line_offset(
        source_text=prepared_program.source_text,
        body_text=prepared_program.body_text,
    )
    requests: list[_RemoteEntryRequest] = []
    for use in live_program.parsed.uses:
        kind = cast(EntryKind, use.kind)
        if kind not in CAP_KINDS:
            continue
        if kinds is not None and kind not in kinds:
            continue
        requests.append(
            _RemoteEntryRequest(
                visibility="private",
                kind=kind,
                ref=use.reference,
                name=None,
                relative_config_path=relative_program_path,
                config_path=program_path,
                form="ref",
                source_line=use.span.line + line_offset,
            )
        )
    return _materialize_remote_entry_requests(
        durable.toolang_root,
        durable.agent_name,
        tuple(requests),
        materialize=materialize,
        remote_cache=remote_cache,
        progress=progress,
    )


def _collect_program_embedded_entries(
    durable: DurableState,
    *,
    visibility: PreparedVisibility | None = None,
    kinds: set[EntryKind] | None = None,
    materialize: bool = False,
) -> tuple[tuple[PreparedEntry, ...], dict[str, bytes]]:
    del materialize
    if visibility == "shared" or durable.program_source is None:
        return (), {}
    prepared_program = build_prepared_program(durable)
    live_program = load_live_program(prepared_program)
    relative_program_path = Path(prepared_program.source_path)
    program_path = durable.toolang_root / relative_program_path
    line_offset = _program_body_line_offset(
        source_text=prepared_program.source_text,
        body_text=prepared_program.body_text,
    )
    entries: list[PreparedEntry] = []
    files: dict[str, bytes] = {}
    seen: dict[tuple[EntryKind, str], int] = {}
    for cap in live_program.parsed.caps:
        kind = _embedded_cap_kind(cap)
        if kind is None:
            continue
        if kinds is not None and kind not in kinds:
            continue
        key = (kind, cap.name)
        existing_line = seen.get(key)
        if existing_line is not None:
            raise ValueError(
                f"duplicate embedded {kind} cap: {cap.name} "
                f"(lines {existing_line} and {cap.span.line + line_offset})"
            )
        seen[key] = cap.span.line + line_offset
        entry, entry_files = _embedded_entry_from_cap(
            durable.toolang_root,
            durable.agent_name,
            kind=kind,
            cap=cap,
            relative_program_path=relative_program_path,
            program_path=program_path,
            source_line=cap.span.line + line_offset,
        )
        entries.append(entry)
        files.update(entry_files)
    return tuple(sorted(entries, key=_entry_sort_key)), files


def _embedded_cap_kind(cap: CapDecl) -> EntryKind | None:
    if cap.kind not in EMBEDDED_CAP_KINDS:
        return None
    return cast(EntryKind, cap.kind)


def _embedded_entry_from_cap(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: EntryKind,
    cap: CapDecl,
    relative_program_path: Path,
    program_path: Path,
    source_line: int,
) -> tuple[PreparedEntry, dict[str, bytes]]:
    del toolang_root
    relative_entry_path = _relative_embedded_entry_path(agent_name, kind=kind, name=cap.name)
    content = _embedded_materialized_content(cap)
    return (
        PreparedEntry(
            kind=kind,
            name=cap.name,
            shape="file",
            ref=f"inline://{DIR_NAME_BY_KIND[kind]}/{cap.name}",
            path=str(relative_entry_path),
            source=_source_record(
                root_relative_path=relative_program_path,
                absolute_path=program_path,
                origin="local",
                form="inline",
                shape="file",
                line=source_line,
            ),
            meta=_load_meta_text(content.decode("utf-8")),
        ),
        {str(relative_entry_path): content},
    )


def _relative_embedded_entry_path(
    agent_name: str,
    *,
    kind: EntryKind,
    name: str,
) -> Path:
    return Path("agents") / agent_name / ".caps" / "inline" / DIR_NAME_BY_KIND[kind] / f"{name}.md"


def _embedded_materialized_content(cap: CapDecl) -> bytes:
    if not cap.meta:
        return cap.body.encode("utf-8")
    post = frontmatter.Post(cap.body, **dict(cap.meta))
    return frontmatter.dumps(post).encode("utf-8")


def _program_body_line_offset(*, source_text: str, body_text: str) -> int:
    body_lines = body_text.splitlines()
    if not body_lines:
        return 0
    source_lines = source_text.splitlines()
    body_len = len(body_lines)
    for index in range(0, len(source_lines) - body_len + 1):
        if source_lines[index : index + body_len] == body_lines:
            return index
    return 0


def _remote_entry_from_ref(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    ref: str,
    name: str | None,
    relative_config_path: Path,
    config_path: Path,
    form: Literal["wired", "ref"],
    source_line: int | None = None,
    materialize: bool,
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
) -> tuple[PreparedEntry, dict[str, bytes]]:
    request = _RemoteEntryRequest(
        visibility=visibility,
        kind=kind,
        ref=ref,
        name=name,
        relative_config_path=relative_config_path,
        config_path=config_path,
        form=form,
        source_line=source_line,
    )
    cached = _cached_remote_entry(remote_cache, request)
    if cached is not None:
        canonical_ref = cached.ref
        _emit_cached_remote_progress(request, canonical_ref, progress=progress)
    elif materialize and "://" not in ref:
        canonical_ref = _resolve_remote_ref(kind, ref, progress=progress)
    else:
        canonical_ref = _canonicalize_remote_ref(kind, ref)
    if name is None:
        name = _remote_name(kind, canonical_ref)
    relative_entry_path = _relative_remote_entry_path(
        agent_name,
        visibility=visibility,
        kind=kind,
        name=name,
        form=form,
    )
    if cached is not None:
        entry_files = _remap_cached_remote_files(cached, relative_entry_path)
    elif materialize and progress is not None:
        entry_files = _remote_materialized_files(
            relative_entry_path=relative_entry_path,
            kind=kind,
            name=name,
            ref=canonical_ref,
            progress=progress,
        )
    elif materialize:
        entry_files = _remote_materialized_files(
            relative_entry_path=relative_entry_path,
            kind=kind,
            name=name,
            ref=canonical_ref,
        )
    else:
        entry_files = {
            str(relative_entry_path): _remote_placeholder_content(
                kind=kind,
                name=name,
                ref=canonical_ref,
            )
        }
    if materialize and cached is None:
        emit_progress(
            progress,
            id=f"cap.extract:{kind}:{canonical_ref}",
            phase="cap.extract",
            label=f"Extract {kind}",
            status="running",
            detail=str(relative_entry_path),
        )
    try:
        entry_content = entry_files[str(relative_entry_path)]
        entry = PreparedEntry(
            kind=kind,
            name=name,
            shape="dir" if kind == "skill" else "file",
            ref=canonical_ref,
            path=str(relative_entry_path),
            source=_source_record(
                root_relative_path=relative_config_path,
                absolute_path=config_path,
                origin="remote",
                form=form,
                shape="file",
                line=source_line,
            ),
            meta=_load_meta_text(entry_content.decode("utf-8")),
        )
    except Exception as exc:
        if materialize and cached is None:
            emit_progress(
                progress,
                id=f"cap.extract:{kind}:{canonical_ref}",
                phase="cap.extract",
                label=f"Extract {kind}",
                status="failed",
                detail=str(exc),
        )
        raise
    if materialize and cached is None:
        emit_progress(
            progress,
            id=f"cap.extract:{kind}:{canonical_ref}",
            phase="cap.extract",
            label=f"Extract {kind}",
            status="ok",
        )
        emit_progress(
            progress,
            id=f"cap.materialize:{kind}:{canonical_ref}",
            phase="cap.materialize",
            label=f"Materialize {kind}",
            status="running",
            detail=str(relative_entry_path),
        )
        emit_progress(
            progress,
            id=f"cap.materialize:{kind}:{canonical_ref}",
            phase="cap.materialize",
            label=f"Materialize {kind}",
            status="ok",
        )
    return entry, entry_files


def _materialize_remote_entry_requests(
    toolang_root: Path,
    agent_name: str,
    requests: tuple[_RemoteEntryRequest, ...],
    *,
    materialize: bool,
    remote_cache: _RemoteEntryCache | None,
    progress: ProgressSink | None,
) -> tuple[tuple[PreparedEntry, ...], dict[str, bytes]]:
    if not requests:
        return (), {}
    if not materialize:
        return _materialize_remote_entry_requests_serial(
            toolang_root,
            agent_name,
            requests,
            materialize=materialize,
            remote_cache=remote_cache,
            progress=progress,
        )
    for request in requests:
        _emit_remote_entry_pending(request, progress=progress)
    entries: list[PreparedEntry] = []
    files: dict[str, bytes] = {}
    results: list[tuple[PreparedEntry, dict[str, bytes]] | None] = [None] * len(requests)
    first_error: BaseException | None = None
    executor = ThreadPoolExecutor(max_workers=REMOTE_CAP_MATERIALIZE_WORKERS)
    try:
        futures = {
            executor.submit(
                _remote_entry_from_request,
                toolang_root,
                agent_name,
                request,
                materialize=materialize,
                remote_cache=remote_cache,
                progress=progress,
            ): index
            for index, request in enumerate(requests)
        }
        try:
            for future in as_completed(futures):
                try:
                    results[futures[future]] = future.result()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    if first_error is not None:
        raise first_error
    for result in results:
        if result is None:
            continue
        entry, entry_files = result
        entries.append(entry)
        files.update(entry_files)
    return tuple(sorted(entries, key=_entry_sort_key)), files


def _materialize_remote_entry_requests_serial(
    toolang_root: Path,
    agent_name: str,
    requests: tuple[_RemoteEntryRequest, ...],
    *,
    materialize: bool,
    remote_cache: _RemoteEntryCache | None,
    progress: ProgressSink | None,
) -> tuple[tuple[PreparedEntry, ...], dict[str, bytes]]:
    entries: list[PreparedEntry] = []
    files: dict[str, bytes] = {}
    for request in requests:
        entry, entry_files = _remote_entry_from_request(
            toolang_root,
            agent_name,
            request,
            materialize=materialize,
            remote_cache=remote_cache,
            progress=progress,
        )
        entries.append(entry)
        files.update(entry_files)
    return tuple(sorted(entries, key=_entry_sort_key)), files


def _remote_entry_from_request(
    toolang_root: Path,
    agent_name: str,
    request: _RemoteEntryRequest,
    *,
    materialize: bool,
    remote_cache: _RemoteEntryCache | None,
    progress: ProgressSink | None,
) -> tuple[PreparedEntry, dict[str, bytes]]:
    return _remote_entry_from_ref(
        toolang_root,
        agent_name,
        visibility=request.visibility,
        kind=request.kind,
        ref=request.ref,
        name=request.name,
        relative_config_path=request.relative_config_path,
        config_path=request.config_path,
        form=request.form,
        source_line=request.source_line,
        materialize=materialize,
        remote_cache=remote_cache,
        progress=progress,
    )


def _emit_remote_entry_pending(
    request: _RemoteEntryRequest,
    *,
    progress: ProgressSink | None,
) -> None:
    ref = request.ref.strip()
    if "://" in ref:
        try:
            ref = _canonicalize_remote_ref(request.kind, ref)
        except ValueError:
            pass
        emit_progress(
            progress,
            id=f"cap.fetch:{request.kind}:{ref}",
            phase="cap.fetch",
            label=f"Fetch {request.kind}",
            status="pending",
            detail=ref,
        )
        return
    emit_progress(
        progress,
        id=f"cap.resolve:{request.kind}:{ref}",
        phase="cap.resolve",
        label=f"Resolve {request.kind}",
        status="pending",
        detail=ref,
    )


def _relative_remote_entry_path(
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    name: str,
    form: Literal["wired", "ref"],
) -> Path:
    prefix = Path(".caps") if visibility == "shared" else Path("agents") / agent_name / ".caps"
    root = prefix / form / DIR_NAME_BY_KIND[kind] / name
    if kind == "skill":
        return root / "SKILL.md"
    return root.with_suffix(".md")


def _remote_placeholder_content(
    *,
    kind: EntryKind,
    name: str,
    ref: str,
) -> bytes:
    post = frontmatter.Post(
        f"Remote {kind} materialized from {ref}\n",
        name=name,
        ref=ref,
        remote=True,
    )
    return frontmatter.dumps(post).encode("utf-8")


def _remote_materialized_files(
    *,
    relative_entry_path: Path,
    kind: EntryKind,
    name: str,
    ref: str,
    progress: ProgressSink | None = None,
) -> dict[str, bytes]:
    del name
    if not ref.startswith("github://"):
        raise ValueError(f"unsupported remote {kind} ref: {ref}")
    github_ref = _parse_github_remote_ref(ref)
    emit_progress(
        progress,
        id=f"cap.fetch:{kind}:{ref}",
        phase="cap.fetch",
        label=f"Fetch {kind}",
        status="running",
        detail=ref,
    )
    if kind == "skill":
        try:
            files = _fetch_github_directory(github_ref)
        except Exception as exc:
            emit_progress(
                progress,
                id=f"cap.fetch:{kind}:{ref}",
                phase="cap.fetch",
                label=f"Fetch {kind}",
                status="failed",
                detail=str(exc),
            )
            raise
        if "SKILL.md" not in files:
            emit_progress(
                progress,
                id=f"cap.fetch:{kind}:{ref}",
                phase="cap.fetch",
                label=f"Fetch {kind}",
                status="failed",
                detail=f"remote skill is missing SKILL.md: {ref}",
            )
            raise ValueError(f"remote skill is missing SKILL.md: {ref}")
        root = relative_entry_path.parent
        materialized = {str(root / relative_path): content for relative_path, content in files.items()}
    else:
        try:
            materialized = {str(relative_entry_path): _fetch_github_file(github_ref)}
        except Exception as exc:
            emit_progress(
                progress,
                id=f"cap.fetch:{kind}:{ref}",
                phase="cap.fetch",
                label=f"Fetch {kind}",
                status="failed",
                detail=str(exc),
            )
            raise
    emit_progress(
        progress,
        id=f"cap.fetch:{kind}:{ref}",
        phase="cap.fetch",
        label=f"Fetch {kind}",
        status="ok",
        detail=f"{len(materialized)} {'file' if len(materialized) == 1 else 'files'}",
    )
    return materialized


def _resolve_remote_ref(kind: EntryKind, ref: str, *, progress: ProgressSink | None = None) -> str:
    text = ref.strip()
    emit_progress(
        progress,
        id=f"cap.resolve:{kind}:{text}",
        phase="cap.resolve",
        label=f"Resolve {kind}",
        status="running",
        detail=text,
    )
    if "://" in text:
        try:
            canonical_ref = _canonicalize_remote_ref(kind, text)
            if canonical_ref.startswith("github://") and not _github_remote_exists(kind, canonical_ref):
                raise ValueError(f"remote {kind} not found or missing entry file: {ref}")
        except Exception as exc:
            emit_progress(
                progress,
                id=f"cap.resolve:{kind}:{text}",
                phase="cap.resolve",
                label=f"Resolve {kind}",
                status="failed",
                detail=str(exc),
            )
            raise
        emit_progress(
            progress,
            id=f"cap.resolve:{kind}:{text}",
            phase="cap.resolve",
            label=f"Resolve {kind}",
            status="ok",
            detail=canonical_ref,
        )
        return canonical_ref
    candidates = _remote_ref_candidates(kind, text)
    if not candidates:
        emit_progress(
            progress,
            id=f"cap.resolve:{kind}:{text}",
            phase="cap.resolve",
            label=f"Resolve {kind}",
            status="failed",
            detail=f"invalid remote ref: {ref}",
        )
        raise ValueError(f"invalid remote ref: {ref}")
    for candidate in candidates:
        if _github_remote_exists(kind, candidate):
            emit_progress(
                progress,
                id=f"cap.resolve:{kind}:{text}",
                phase="cap.resolve",
                label=f"Resolve {kind}",
                status="ok",
                detail=candidate,
            )
            return candidate
    emit_progress(
        progress,
        id=f"cap.resolve:{kind}:{text}",
        phase="cap.resolve",
        label=f"Resolve {kind}",
        status="failed",
        detail=f"could not resolve remote {kind} shorthand: {ref}",
    )
    raise ValueError(f"could not resolve remote {kind} shorthand: {ref}")


def _canonicalize_remote_ref(kind: EntryKind, ref: str) -> str:
    text = ref.strip()
    if "://" in text:
        github_ref = _github_remote_ref_from_url(text)
        if github_ref is not None:
            if kind == "skill" and Path(github_ref.path).name == "SKILL.md":
                github_ref = _GitHubRemoteRef(
                    owner=github_ref.owner,
                    repo=github_ref.repo,
                    path=str(Path(github_ref.path).parent),
                    rev=github_ref.rev,
                )
            return github_ref.render()
        if text.startswith("github://"):
            github_ref = _parse_github_remote_ref(text)
            if kind == "skill" and Path(github_ref.path).name == "SKILL.md":
                github_ref = _GitHubRemoteRef(
                    owner=github_ref.owner,
                    repo=github_ref.repo,
                    path=str(Path(github_ref.path).parent),
                    rev=github_ref.rev,
                )
            return github_ref.render()
        return text
    candidates = _remote_ref_candidates(kind, text)
    if not candidates:
        raise ValueError(f"invalid remote ref: {ref}")
    return candidates[0]


def _remote_ref_candidates(kind: EntryKind, ref: str) -> tuple[str, ...]:
    slash_count = ref.count("/")
    if slash_count == 2:
        owner, repo, name = ref.split("/", 2)
        if not owner or not repo or not name:
            return ()
        return _remote_ref_candidates_for_repo(kind, owner, repo, name)
    if slash_count != 1:
        return ()
    parts = ref.split("/", 1)
    if not parts[0] or not parts[1]:
        return ()
    owner, name = parts
    if kind == "skill":
        return _remote_ref_existing_repo_candidates(
            _github_remote_ref_with_default_branch(owner, "agents", f"skills/{name}"),
            _github_remote_ref_with_default_branch(owner, "agent-skills", name),
            _github_remote_ref_with_default_branch(owner, "agent-skills", f"skills/{name}"),
            _github_remote_ref_with_default_branch(owner, "skills", name),
            _github_remote_ref_with_default_branch(owner, "skills", f"skills/{name}"),
        )
    if kind == "service":
        return _remote_ref_existing_repo_candidates(
            _github_remote_ref_with_default_branch(owner, "agents", f"services/{name}.md"),
            _github_remote_ref_with_default_branch(owner, "agent-services", f"{name}.md"),
            _github_remote_ref_with_default_branch(owner, "services", f"{name}.md"),
        )
    if kind == "prompt":
        return _remote_ref_existing_repo_candidates(
            _github_remote_ref_with_default_branch(owner, "agents", f"prompts/{name}.md"),
            _github_remote_ref_with_default_branch(owner, "agent-prompts", f"{name}.md"),
            _github_remote_ref_with_default_branch(owner, "prompts", f"{name}.md"),
        )
    if kind == "psyche":
        return _remote_ref_existing_repo_candidates(
            _github_remote_ref_with_default_branch(owner, "agents", f"psyches/{name}.md"),
            _github_remote_ref_with_default_branch(owner, "agent-psyches", f"{name}.md"),
            _github_remote_ref_with_default_branch(owner, "psyches", f"{name}.md"),
        )
    return ()


def _remote_ref_candidates_for_repo(kind: EntryKind, owner: str, repo: str, name: str) -> tuple[str, ...]:
    return _remote_ref_existing_repo_candidates(
        *(
            _github_remote_ref_with_default_branch(owner, repo, path)
            for path in _remote_path_candidates_for_repo(kind, repo, name)
        )
    )


def _remote_path_candidates_for_repo(kind: EntryKind, repo: str, name: str) -> tuple[str, ...]:
    if kind == "skill":
        if repo in {"agent-skills", "skills"}:
            return (name, f"skills/{name}")
        return (f"skills/{name}", name)
    if kind in {"psyche", "service", "prompt"}:
        directory = DIR_NAME_BY_KIND[kind]
        if repo in {f"agent-{directory}", directory}:
            return (f"{name}.md",)
        return (f"{directory}/{name}.md", f"{name}.md")
    return ()


def _remote_ref_existing_repo_candidates(*refs: str | None) -> tuple[str, ...]:
    return tuple(ref for ref in refs if ref is not None)


def _github_remote_ref_with_default_branch(owner: str, repo: str, path: str) -> str | None:
    try:
        rev = _github_repo_default_branch(owner, repo)
    except ValueError:
        rev = "main"
    return _GitHubRemoteRef(owner=owner, repo=repo, path=path, rev=rev).render()


def _github_remote_exists(kind: EntryKind, ref: str) -> bool:
    github_ref = _parse_github_remote_ref(ref)
    probe_ref = github_ref
    if kind == "skill":
        probe_ref = _GitHubRemoteRef(
            owner=github_ref.owner,
            repo=github_ref.repo,
            path=str(Path(github_ref.path) / "SKILL.md"),
            rev=github_ref.rev,
        )
    return _github_raw_file_exists(probe_ref)


def _parse_github_remote_ref(text: str) -> _GitHubRemoteRef:
    parsed = urlsplit(text)
    if parsed.scheme != "github":
        raise ValueError(f"unsupported remote ref: {text}")
    owner = parsed.netloc.strip()
    path_text = parsed.path.strip("/")
    if not owner or not path_text or "/" not in path_text:
        raise ValueError(f"invalid GitHub remote ref: {text}")
    repo, _, repo_path = path_text.partition("/")
    if not repo or not repo_path:
        raise ValueError(f"invalid GitHub remote ref: {text}")
    path = repo_path
    rev: str | None = None
    if "@" in repo_path:
        path, _, rev = repo_path.rpartition("@")
        if not path or not rev:
            raise ValueError(f"invalid GitHub remote ref: {text}")
    if rev is None:
        raise ValueError(f"GitHub remote ref must include @rev: {text}")
    return _GitHubRemoteRef(owner=owner, repo=repo, path=path, rev=rev)


def _github_remote_ref_from_url(text: str) -> _GitHubRemoteRef | None:
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc == "github.com":
        return _github_remote_ref_from_web_url(parsed.path, text)
    if parsed.netloc == "raw.githubusercontent.com":
        return _github_remote_ref_from_raw_url(parsed.path, text)
    return None


def _github_remote_ref_from_web_url(path_text: str, original: str) -> _GitHubRemoteRef:
    parts = [part for part in path_text.strip("/").split("/") if part]
    if len(parts) < 5 or parts[2] not in {"tree", "blob"}:
        raise ValueError(f"invalid GitHub remote ref: {original}")
    owner, repo = parts[:2]
    rev, path = _split_github_url_rev_and_path(parts[3:], original)
    if not owner or not repo or not rev or not path:
        raise ValueError(f"invalid GitHub remote ref: {original}")
    return _GitHubRemoteRef(owner=owner, repo=repo, path=path, rev=rev)


def _github_remote_ref_from_raw_url(path_text: str, original: str) -> _GitHubRemoteRef:
    parts = [part for part in path_text.strip("/").split("/") if part]
    if len(parts) < 4:
        raise ValueError(f"invalid GitHub remote ref: {original}")
    owner, repo = parts[:2]
    rev, path = _split_github_url_rev_and_path(parts[2:], original)
    if not owner or not repo or not rev or not path:
        raise ValueError(f"invalid GitHub remote ref: {original}")
    return _GitHubRemoteRef(owner=owner, repo=repo, path=path, rev=rev)


def _split_github_url_rev_and_path(parts: list[str], original: str) -> tuple[str, str]:
    if len(parts) >= 4 and parts[0] == "refs" and parts[1] in {"heads", "tags"}:
        return "/".join(parts[:3]), "/".join(parts[3:])
    if len(parts) >= 2:
        return parts[0], "/".join(parts[1:])
    raise ValueError(f"invalid GitHub remote ref: {original}")


@lru_cache
def _github_repo_default_branch(owner: str, repo: str) -> str:
    api_url = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    data = _fetch_json(api_url)
    if not isinstance(data, dict):
        raise ValueError(f"unexpected GitHub repository response: {owner}/{repo}")
    repo_data = cast(dict[str, object], data)
    default_branch = repo_data.get("default_branch")
    if not isinstance(default_branch, str):
        raise ValueError(f"unexpected GitHub repository response: {owner}/{repo}")
    return default_branch


def _fetch_github_directory(ref: _GitHubRemoteRef) -> dict[str, bytes]:
    root = ref.path.strip("/")
    prefix = f"{root}/"
    archive_url = (
        f"https://codeload.github.com/{quote(ref.owner, safe='')}/"
        f"{quote(ref.repo, safe='')}/tar.gz/{quote(ref.rev, safe='')}"
    )
    archive_bytes = _fetch_url_bytes(archive_url)
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            member_path = Path(member.name)
            path = "/".join(member_path.parts[1:])
            if not path.startswith(prefix):
                continue
            relative_path = path.removeprefix(prefix)
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            files[relative_path] = extracted.read()
    if not files:
        raise ValueError(f"could not fetch remote directory: {ref.render()}")
    return files


def _github_raw_file_exists(ref: _GitHubRemoteRef) -> bool:
    request = Request(_github_raw_url(ref), method="HEAD", headers={"User-Agent": "toolang/0.1"})
    try:
        with urlopen(request, timeout=30):
            return True
    except (HTTPError, URLError):
        return False


def _github_raw_url(ref: _GitHubRemoteRef) -> str:
    rev = quote(ref.rev, safe="/")
    path = quote(ref.path.lstrip("/"), safe="/")
    return f"https://raw.githubusercontent.com/{ref.owner}/{ref.repo}/{rev}/{path}"


def _fetch_github_file(ref: _GitHubRemoteRef) -> bytes:
    return _fetch_url_bytes(_github_raw_url(ref))


def _fetch_github_file_from_api(ref: _GitHubRemoteRef) -> bytes:
    data = _github_contents_json(ref)
    if isinstance(data, list) or data.get("type") != "file":
        raise ValueError(f"remote ref is not a file: {ref.render()}")
    content = data.get("content")
    encoding = data.get("encoding")
    if isinstance(content, str) and encoding == "base64":
        return base64.b64decode(content)
    download_url = data.get("download_url")
    if isinstance(download_url, str) and download_url:
        return _fetch_url_bytes(download_url)
    raise ValueError(f"could not decode remote file: {ref.render()}")


def _github_contents_json(ref: _GitHubRemoteRef) -> dict[str, object] | list[dict[str, object]]:
    path = quote(ref.path.lstrip("/"), safe="/")
    api_url = f"https://api.github.com/repos/{ref.owner}/{ref.repo}/contents/{path}"
    api_url = f"{api_url}?ref={quote(ref.rev, safe='')}"
    data = _fetch_json(api_url)
    if isinstance(data, dict | list):
        return cast(dict[str, object] | list[dict[str, object]], data)
    raise ValueError(f"unexpected GitHub response for {ref.render()}")


def _github_tree_json(ref: _GitHubRemoteRef) -> dict[str, object]:
    branch = quote(ref.rev, safe="")
    api_url = f"https://api.github.com/repos/{ref.owner}/{ref.repo}/git/trees/{branch}?recursive=1"
    data = _fetch_json(api_url)
    if isinstance(data, dict):
        return cast(dict[str, object], data)
    raise ValueError(f"unexpected GitHub tree response for {ref.render()}")


def _fetch_json(url: str) -> object:
    return json.loads(_fetch_url_bytes(url).decode("utf-8"))


def _fetch_url_bytes(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "toolang/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return response.read()
    except (HTTPError, URLError) as exc:
        raise ValueError(f"could not fetch remote content: {url}") from exc


def _legacy_fetch_github_directory(ref: _GitHubRemoteRef) -> dict[str, bytes]:
    tree = _github_tree_json(ref)
    root = ref.path.strip("/")
    prefix = f"{root}/"
    files: dict[str, bytes] = {}
    for item in cast(list[dict[str, object]], tree.get("tree", [])):
        if item.get("type") != "blob":
            continue
        path = str(item.get("path", ""))
        if not path.startswith(prefix):
            continue
        relative_path = path.removeprefix(prefix)
        if not relative_path:
            continue
        files[relative_path] = _fetch_github_file_from_api(
            _GitHubRemoteRef(
                owner=ref.owner,
                repo=ref.repo,
                path=path,
                rev=ref.rev,
            )
        )
    if not files:
        raise ValueError(f"could not fetch remote directory: {ref.render()}")
    return files


def _remote_name(kind: EntryKind, ref: str) -> str:
    if ref.startswith("github://"):
        path = _parse_github_remote_ref(ref).path.rstrip("/")
        if not path:
            raise ValueError(f"invalid remote ref: {ref}")
        if kind == "skill":
            return Path(path).name
        return Path(path).stem
    parsed = urlparse(ref)
    path = parsed.path.rstrip("/")
    if not path:
        raise ValueError(f"invalid remote ref: {ref}")
    name = Path(path).name
    if "@" in name:
        name = name.split("@", 1)[0]
    if kind == "skill":
        return name
    return Path(name).stem


def _config_path(toolang_root: Path, agent_name: str, *, visibility: PreparedVisibility) -> Path:
    if visibility == "shared":
        return toolang_root / "config.toml"
    return toolang_root / "agents" / agent_name / "config.toml"


def _load_config_data(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    return cast(dict[str, object], tomllib.loads(path.read_text(encoding="utf-8")))


def _write_config_data(path: Path, data: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_config(dict(data)), encoding="utf-8")


def _render_config(data: dict[str, object]) -> str:
    remote_section_names = {DIR_NAME_BY_KIND[kind] for kind in CAP_KINDS}
    standard_data = {
        key: value
        for key, value in data.items()
        if key not in remote_section_names
    }
    remote_data = {
        key: value
        for key, value in data.items()
        if key in remote_section_names
    }

    parts: list[str] = []
    standard_text = tomli_w.dumps(standard_data).strip()
    if standard_text:
        parts.append(standard_text)

    ctx = TomlContext(allow_multiline=False, indent=4)
    lines: list[str] = []
    items = sorted(remote_data.items(), key=lambda item: (CONFIG_SECTION_ORDER.get(item[0], 999), item[0]))
    for key, value in items:
        if isinstance(value, dict):
            lines.append(f"[{format_key_part(key)}]")
            for entry_name, entry_value in sorted(value.items()):
                if not isinstance(entry_value, Mapping):
                    raise TypeError(f"invalid config entry for {key}.{entry_name}: {entry_value!r}")
                rendered = format_inline_table(cast(Mapping[str, object], entry_value), ctx)
                lines.append(f"{format_key_part(str(entry_name))} = {rendered}")
            lines.append("")
            continue
        lines.append(f"{format_key_part(key)} = {format_literal(value, ctx)}")
    remote_text = "\n".join(lines).rstrip()
    if remote_text:
        parts.append(remote_text)
    return "\n\n".join(parts).rstrip() + ("\n" if parts else "")


def _config_kind_table(data: dict[str, object], kind: EntryKind) -> dict[str, object]:
    key = DIR_NAME_BY_KIND[kind]
    table = data.get(key)
    if isinstance(table, dict):
        return cast(dict[str, object], table)
    new_table: dict[str, object] = {}
    data[key] = new_table
    return new_table


def _config_kind_table_optional(data: dict[str, object], kind: EntryKind) -> dict[str, object] | None:
    table = data.get(DIR_NAME_BY_KIND[kind])
    if isinstance(table, dict):
        return cast(dict[str, object], table)
    return None


def _config_ref(item: object) -> str:
    if isinstance(item, dict):
        ref = cast(dict[str, object], item).get("ref")
        if isinstance(ref, str) and ref:
            return ref
    raise ValueError(f"invalid remote cap config entry: {item!r}")


def _remove_remote_entries_by_name(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    name: str,
) -> bool:
    if kind in JOB_KINDS:
        return False
    config_path = _config_path(toolang_root, agent_name, visibility=visibility)
    if not config_path.is_file():
        return False
    data = _load_config_data(config_path)
    key = DIR_NAME_BY_KIND[kind]
    kind_table = _config_kind_table_optional(data, kind)
    if kind_table is None or name not in kind_table:
        return False
    kind_table.pop(name, None)
    if not kind_table:
        data.pop(key, None)
    _write_config_data(config_path, data)
    return True
