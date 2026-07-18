"""Prepared cap state and remote materialization."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tarfile
from typing import Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import frontmatter

from toolang.catalog import cap as cap_catalog
from toolang.state.durable import DurableFile, DurableState, scan_durable_state
from ..common.progress import ProgressSink, emit_progress
from toolang.state.prepared import (
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
from ..lang.ast import CapDecl
from toolang.common.selectors import (
    Selector,
    filter_value_matches,
    parse_selector,
    split_selector_list,
    selector_identity_matches,
)
from toolang.common.github import GitHubRef, github_raw_url, parse_github_ref

CAP_DIR_NAMES = ("psyches", "skills", "services", "prompts")
CAP_KINDS: tuple[EntryKind, ...] = ("psyche", "skill", "service", "prompt")
Visibility = PreparedVisibility
EntryOrigin = SourceOrigin
EntryForm = SourceForm
EntryScope = Literal["root", "home", "here"]
EMBEDDED_CAP_KINDS = frozenset({"psyche", "service", "prompt"})
FILE_BACKED_KINDS = frozenset({"psyche", "service", "prompt"})
DIR_NAME_BY_KIND: dict[EntryKind, str] = {
    "psyche": "psyches",
    "skill": "skills",
    "service": "services",
    "prompt": "prompts",
}
KIND_BY_DIR_NAME = {
    "psyches": "psyche",
    "skills": "skill",
    "services": "service",
    "prompts": "prompt",
}
REMOTE_CAP_MATERIALIZE_WORKERS = 4


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
    """List effective cap entries projected from durable authored state."""

    durable = scan_durable_state(toolang_root, agent_name)
    entries, _ = _collect_visibility_entries_with_files(
        durable, visibility=visibility, kinds=kinds
    )
    return entries


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
    if not selector_identity_matches(
        family=entry.kind, name=entry.name, selector=parsed
    ):
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
        entry_visibility_value: PreparedVisibility = (
            "shared" if item.origin == "root" else "private"
        )
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
    entries = _dedupe_entries(
        (*local_entries, *remote_entries, *embedded_entries, *use_entries)
    )
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
            input_fingerprint=visibility_input_fingerprint(
                durable, visibility=visibility
            ),
            entries=entries,
            program_source=None,
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
        if manifest is not None and not _entry_artifact_matches_manifest(
            toolang_root, entry, manifest
        ):
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
            authored_ref=_entry_authored_ref(manifest, entry)
            if manifest is not None
            else None,
            source_fingerprint=entry.source.fingerprint,
            files=files,
        )
    return cache


def _load_lock_manifest_optional(lock: PreparedLock) -> dict[str, object] | None:
    try:
        return cast(
            dict[str, object], json.loads(lock.lock_path.read_text(encoding="utf-8"))
        )
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
        return entry_path.is_file() and item.get("fingerprint") == _file_fingerprint(
            entry_path
        )
    if not entry_path.parent.is_dir():
        return False
    expected = _artifact_child_fingerprints(item)
    actual = {
        str(path.relative_to(entry_path.parent)): _file_fingerprint(path)
        for path in sorted(
            child for child in entry_path.parent.rglob("*") if child.is_file()
        )
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
    for item in cast(list[dict[str, object]], prepared.get("caps", [])):
        if item.get("kind") != entry.kind or item.get("name") != entry.name:
            continue
        origin = item.get("origin")
        if (
            isinstance(origin, dict)
            and cast(dict[str, object], origin).get("ref") != entry.ref
        ):
            continue
        artifact_index = item.get("artifact")
        if not isinstance(artifact_index, int):
            return None
        bucket = cast(
            dict[str, object],
            cast(dict[str, object], manifest.get("artifacts", {})).get(
                entry.source.form, {}
            ),
        )
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
    if (
        "://" in request.ref
        and cap_catalog.canonicalize_remote_ref(request.kind, request.ref) == cached.ref
    ):
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
    return {
        str(root / relative_path): content for relative_path, content in cached.files
    }


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

    return _durable_files_fingerprint(
        _visibility_input_files(durable, visibility=visibility)
    )


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


def durable_entries_snapshot(
    durable: DurableState,
) -> dict[str, object]:
    """Return a JSON-friendly durable definitions snapshot."""

    shared_entries, _ = _collect_visibility_entries_with_files(
        durable, visibility="shared"
    )
    private_entries, _ = _collect_visibility_entries_with_files(
        durable, visibility="private"
    )
    return {
        "program_source": durable.program_path,
        "config_paths": list(durable.config_paths),
        "shared_entries": [entry.to_snapshot() for entry in shared_entries],
        "private_entries": [entry.to_snapshot() for entry in private_entries],
    }


def _local_entry_from_file(
    toolang_root: Path,
    agent_name: str,
    item: DurableFile,
) -> PreparedEntry | None:
    if item.category != "cap":
        return None
    visibility: PreparedVisibility = "shared" if item.origin == "root" else "private"
    relative_path = Path(item.relative_path)
    local_parts = _local_parts(
        relative_path, agent_name=agent_name, visibility=visibility
    )
    if len(local_parts) < 2:
        return None
    directory_name = local_parts[0]
    kind = cast(EntryKind | None, KIND_BY_DIR_NAME.get(directory_name))
    if kind is None:
        return None
    if kind == "skill":
        return _skill_entry(
            toolang_root, agent_name, visibility=visibility, name=local_parts[1]
        )
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
    root_relative_dir = cap_catalog.relative_definition_root(
        agent_name, visibility=visibility, kind="skill", name=name
    )
    root_relative_file = root_relative_dir / "SKILL.md"
    entry_file = toolang_root / root_relative_file
    if not entry_file.is_file():
        return None
    source_path = toolang_root / root_relative_dir
    return PreparedEntry(
        kind="skill",
        name=name,
        shape="dir",
        ref=cap_catalog.local_cap_ref(
            visibility=visibility,
            kind="skill",
            name=name,
        ),
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
        ref=cap_catalog.local_cap_ref(
            visibility=_visibility_from_relative_path(relative_path),
            kind=kind,
            name=relative_path.stem,
        ),
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
    fingerprint = (
        _dir_fingerprint(absolute_path)
        if shape == "dir"
        else _file_fingerprint(absolute_path)
    )
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
        if item.origin == "agent"
        and item.category in {"config", "cap", "job", "program"}
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
            "meta": entry.to_data()["meta"],
            "content_fingerprint": _content_fingerprint(
                toolang_root, entry, materialized_files
            ),
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
        return _materialized_dir_fingerprint(
            Path(entry.path).parent, materialized_files
        )
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
        return datetime.fromtimestamp(
            path.stat().st_mtime_ns / 1_000_000_000, tz=timezone.utc
        ).isoformat()
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


def _visibility_from_relative_path(relative_path: Path) -> PreparedVisibility:
    return "private" if relative_path.parts[:1] == ("agents",) else "shared"


def _local_parts(
    relative_path: Path, *, agent_name: str, visibility: PreparedVisibility
) -> tuple[str, ...]:
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
        for entry in cap_catalog.list_wired_entries(
            toolang_root,
            agent_name,
            visibility=item_visibility,
            kinds=kinds,
        ):
            authored_config_path = toolang_root / entry.definition_file
            requests.append(
                _RemoteEntryRequest(
                    visibility=item_visibility,
                    kind=entry.kind,
                    ref=entry.ref,
                    name=entry.name,
                    relative_config_path=Path(entry.definition_file),
                    config_path=authored_config_path,
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
    if visibility == "shared" or durable.program_path is None:
        return (), {}
    program_source = durable.load_program()
    program = program_source.parse()
    relative_program_path = Path(program_source.source_path)
    program_path = durable.toolang_root / relative_program_path
    requests: list[_RemoteEntryRequest] = []
    for use in program.withs:
        kind = use.cap_kind
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
                source_line=use.span.line,
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
    if visibility == "shared" or durable.program_path is None:
        return (), {}
    program_source = durable.load_program()
    program = program_source.parse()
    relative_program_path = Path(program_source.source_path)
    program_path = durable.toolang_root / relative_program_path
    entries: list[PreparedEntry] = []
    files: dict[str, bytes] = {}
    seen: dict[tuple[EntryKind, str], int] = {}
    for cap in program.caps:
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
                f"(lines {existing_line} and {cap.span.line})"
            )
        seen[key] = cap.span.line
        entry, entry_files = _embedded_entry_from_cap(
            durable.toolang_root,
            durable.agent_name,
            kind=kind,
            cap=cap,
            relative_program_path=relative_program_path,
            program_path=program_path,
            source_line=cap.span.line,
        )
        entries.append(entry)
        files.update(entry_files)
    return tuple(sorted(entries, key=_entry_sort_key)), files


def _embedded_cap_kind(cap: CapDecl) -> EntryKind | None:
    if cap.kind not in EMBEDDED_CAP_KINDS:
        return None
    return cap.kind


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
    relative_entry_path = _relative_embedded_entry_path(
        agent_name, kind=kind, name=cap.name
    )
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
    return (
        Path("agents")
        / agent_name
        / ".caps"
        / "inline"
        / DIR_NAME_BY_KIND[kind]
        / f"{name}.md"
    )


def _embedded_materialized_content(cap: CapDecl) -> bytes:
    if not cap.meta:
        return cap.body.encode("utf-8")
    post = frontmatter.Post(cap.body, **dict(cap.meta))
    return frontmatter.dumps(post).encode("utf-8")


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
        canonical_ref = cap_catalog.resolve_remote_ref(kind, ref, progress=progress)
    else:
        canonical_ref = cap_catalog.canonicalize_remote_ref(kind, ref)
    if name is None:
        name = cap_catalog.remote_entry_name(kind, canonical_ref)
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
    results: list[tuple[PreparedEntry, dict[str, bytes]] | None] = [None] * len(
        requests
    )
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
            ref = cap_catalog.canonicalize_remote_ref(request.kind, ref)
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
    prefix = (
        Path(".caps")
        if visibility == "shared"
        else Path("agents") / agent_name / ".caps"
    )
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
    github_ref = parse_github_ref(ref)
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
        materialized = {
            str(root / relative_path): content
            for relative_path, content in files.items()
        }
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


def _fetch_github_directory(ref: GitHubRef) -> dict[str, bytes]:
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


def _fetch_github_file(ref: GitHubRef) -> bytes:
    return _fetch_url_bytes(github_raw_url(ref))


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
