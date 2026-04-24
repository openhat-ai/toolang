"""Caps and local definition helpers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import tomllib
from typing import Literal, cast
from urllib.parse import urlparse

import frontmatter
import tomli_w
from tomli_w._writer import Context as TomlContext
from tomli_w._writer import format_inline_table, format_key_part, format_literal

from .state.durable import DurableFile, DurableState, scan_durable_state
from .state.prepared import (
    EntryKind,
    PreparedEntry,
    PreparedLock,
    PreparedScope,
    PreparedSource,
    agent_lock_path,
    agent_prepared_dir,
    global_lock_path,
    global_prepared_dir,
)

CAP_DIR_NAMES = ("psyches", "skills", "services", "prompts")
JOB_DIR_NAMES = ("chores", "tasks")
CAP_KINDS: tuple[EntryKind, ...] = ("psyche", "skill", "service", "prompt")
JOB_KINDS: tuple[EntryKind, ...] = ("task", "chore")
MANAGED_KINDS = frozenset((*CAP_KINDS, *JOB_KINDS))
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


def list_entries(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: PreparedScope | None = None,
    kinds: set[EntryKind] | None = None,
) -> tuple[PreparedEntry, ...]:
    """List local and remote entries from durable authored files and config."""

    durable = scan_durable_state(toolang_root, agent_name)
    entries, _ = _collect_scope_entries_with_files(durable, scope=scope, kinds=kinds)
    return entries


def list_local_entries(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: PreparedScope | None = None,
    kinds: set[EntryKind] | None = None,
) -> tuple[PreparedEntry, ...]:
    """List local cap and job entries from durable authored files."""

    durable = scan_durable_state(toolang_root, agent_name)
    return collect_local_entries(durable, scope=scope, kinds=kinds)


def put_local_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: PreparedScope,
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
        scope=scope,
        kind=kind,
        name=name,
        text=frontmatter.dumps(post),
    )


def put_local_entry_text(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: PreparedScope,
    kind: EntryKind,
    name: str,
    text: str,
) -> Path:
    """Create or replace one local entry from raw authored text."""

    _validate_local_kind(scope, kind)
    _validate_authored_entry_text(kind=kind, text=text)
    entry_path = _local_entry_file_path(toolang_root, agent_name, scope=scope, kind=kind, name=name)
    ref = entry_path.resolve().as_uri() if kind != "skill" else entry_path.parent.resolve().as_uri()
    _ensure_name_available(toolang_root, agent_name, scope=scope, kind=kind, name=name, ref=ref)
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(text, encoding="utf-8")
    return entry_path


def load_local_entry_text(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: PreparedScope,
    kind: EntryKind,
    name: str,
) -> str:
    """Load one local entry from authored files."""

    _validate_local_kind(scope, kind)
    entry_path = _local_entry_file_path(toolang_root, agent_name, scope=scope, kind=kind, name=name)
    if not entry_path.is_file():
        raise FileNotFoundError(f"local {kind} not found: {name}")
    return entry_path.read_text(encoding="utf-8")


def remove_local_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: PreparedScope,
    kind: EntryKind,
    name: str,
) -> bool:
    """Remove one local entry if it exists."""

    _validate_local_kind(scope, kind)
    if kind == "skill":
        target = toolang_root / _relative_definition_root(agent_name, scope=scope, kind=kind, name=name)
        if not target.exists():
            return False
        shutil.rmtree(target)
        return True
    entry_path = _local_entry_file_path(toolang_root, agent_name, scope=scope, kind=kind, name=name)
    if not entry_path.exists():
        return False
    entry_path.unlink()
    return True


def add_remote_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: PreparedScope,
    kind: EntryKind,
    ref: str,
) -> Path:
    """Add one remote entry ref to the authored config file."""

    _validate_local_kind(scope, kind)
    canonical_ref = _canonicalize_remote_ref(kind, ref)
    name = _remote_name(kind, canonical_ref)
    _ensure_name_available(
        toolang_root,
        agent_name,
        scope=scope,
        kind=kind,
        name=name,
        ref=canonical_ref,
    )
    config_path = _config_path(toolang_root, agent_name, scope=scope)
    data = _load_config_data(config_path)
    key = DIR_NAME_BY_KIND[kind]
    table = data.get(key)
    if isinstance(table, dict):
        kind_table = cast(dict[str, object], table)
    else:
        kind_table = {}
        data[key] = kind_table
    kind_table[name] = {"ref": canonical_ref}
    _write_config_data(config_path, data)
    return config_path


def remove_remote_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: PreparedScope,
    kind: EntryKind,
    name: str,
) -> bool:
    """Remove one remote entry ref by runtime name."""

    _validate_local_kind(scope, kind)
    return _remove_remote_entries_by_name(
        toolang_root,
        agent_name,
        scope=scope,
        kind=kind,
        name=name,
    )


def remote_entry_name(kind: EntryKind, ref: str) -> str:
    """Return the runtime name derived from one remote ref."""

    return _remote_name(kind, _canonicalize_remote_ref(kind, ref))


def collect_local_entries(
    durable: DurableState,
    *,
    scope: PreparedScope | None = None,
    kinds: set[EntryKind] | None = None,
) -> tuple[PreparedEntry, ...]:
    """Collect local prepared entries from durable authored files."""

    entries: dict[str, PreparedEntry] = {}
    for item in durable.files:
        entry = _local_entry_from_file(durable.toolang_root, durable.agent_name, item)
        if entry is None:
            continue
        entry_scope: PreparedScope = "global" if item.origin == "root" else "agent"
        if scope is not None and entry_scope != scope:
            continue
        if kinds is not None and entry.kind not in kinds:
            continue
        entries.setdefault(entry.ref, entry)
    return tuple(sorted(entries.values(), key=_entry_sort_key))


def _collect_scope_entries_with_files(
    durable: DurableState,
    *,
    scope: PreparedScope | None = None,
    kinds: set[EntryKind] | None = None,
) -> tuple[tuple[PreparedEntry, ...], dict[str, bytes]]:
    local_entries = collect_local_entries(durable, scope=scope, kinds=kinds)
    remote_entries, files = _collect_remote_entries(
        durable.toolang_root,
        durable.agent_name,
        scope=scope,
        kinds=kinds,
    )
    entries = tuple(sorted((*local_entries, *remote_entries), key=_entry_sort_key))
    return entries, files


def build_scope_lock(
    durable: DurableState,
    *,
    scope: PreparedScope,
) -> tuple[PreparedLock, dict[str, bytes]]:
    """Build one prepared lock and any materialized files for one scope."""

    entries, files = _collect_scope_entries_with_files(durable, scope=scope)
    _ensure_no_conflicts(entries)
    updated_at = datetime.now(timezone.utc).isoformat()
    if scope == "global":
        prepared_dir = global_prepared_dir(durable.toolang_root)
        lock_path = global_lock_path(durable.toolang_root)
    else:
        prepared_dir = agent_prepared_dir(durable.toolang_root, durable.agent_name)
        lock_path = agent_lock_path(durable.toolang_root, durable.agent_name)
    return (
        PreparedLock(
            scope=scope,
            updated_at=updated_at,
            fingerprint=_lock_fingerprint(durable.toolang_root, entries, files),
            entries=entries,
            program=None,
            prepared_dir=prepared_dir,
            lock_path=lock_path,
            lock_mtime_ns=0,
        ),
        files,
    )


def effective_cap_entries(
    global_lock: PreparedLock,
    agent_lock: PreparedLock,
) -> tuple[PreparedEntry, ...]:
    """Return the runtime-visible cap set after scope precedence is applied."""

    effective: dict[tuple[str, str], PreparedEntry] = {}
    for entry in global_lock.entries:
        if entry.kind in CAP_KINDS:
            effective[(entry.kind, entry.name)] = entry
    for entry in agent_lock.entries:
        if entry.kind in CAP_KINDS:
            effective[(entry.kind, entry.name)] = entry
    return tuple(sorted(effective.values(), key=_entry_sort_key))


def active_job_entries(agent_lock: PreparedLock) -> tuple[PreparedEntry, ...]:
    """Return active runtime job entries."""

    jobs = [
        entry
        for entry in agent_lock.entries
        if entry.kind in JOB_KINDS and entry.meta.get("state") != "archived"
    ]
    return tuple(sorted(jobs, key=_entry_sort_key))


def durable_entries_snapshot(
    durable: DurableState,
) -> dict[str, object]:
    """Return a JSON-friendly durable definitions snapshot."""

    global_entries, _ = _collect_scope_entries_with_files(durable, scope="global")
    agent_entries, _ = _collect_scope_entries_with_files(durable, scope="agent")
    return {
        "program_source": durable.program_source,
        "config_paths": list(durable.config_paths),
        "global_entries": [entry.to_snapshot() for entry in global_entries],
        "agent_entries": [entry.to_snapshot() for entry in agent_entries],
    }


def _local_entry_from_file(
    toolang_root: Path,
    agent_name: str,
    item: DurableFile,
) -> PreparedEntry | None:
    if item.category not in {"cap", "job"}:
        return None
    scope: PreparedScope = "global" if item.origin == "root" else "agent"
    relative_path = Path(item.relative_path)
    local_parts = _local_parts(relative_path, agent_name=agent_name, scope=scope)
    if len(local_parts) < 2:
        return None
    directory_name = local_parts[0]
    kind = cast(EntryKind | None, KIND_BY_DIR_NAME.get(directory_name))
    if kind is None:
        return None
    if kind == "skill":
        return _skill_entry(toolang_root, agent_name, scope=scope, name=local_parts[1])
    if kind in FILE_BACKED_KINDS and len(local_parts) == 2:
        return _file_entry(toolang_root, relative_path, kind=kind)
    return None


def _skill_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: PreparedScope,
    name: str,
) -> PreparedEntry | None:
    root_relative_dir = _relative_definition_root(agent_name, scope=scope, kind="skill", name=name)
    root_relative_file = root_relative_dir / "SKILL.md"
    entry_file = toolang_root / root_relative_file
    if not entry_file.is_file():
        return None
    source_path = toolang_root / root_relative_dir
    return PreparedEntry(
        kind="skill",
        name=name,
        shape="dir",
        ref=source_path.resolve().as_uri(),
        path=str(root_relative_file),
        source=_source_record(
            root_relative_path=root_relative_dir,
            absolute_path=source_path,
            form="local",
            shape="dir",
        ),
        meta=_load_meta(entry_file),
    )


def _file_entry(
    toolang_root: Path,
    relative_path: Path,
    *,
    kind: EntryKind,
) -> PreparedEntry:
    absolute_path = toolang_root / relative_path
    return PreparedEntry(
        kind=kind,
        name=relative_path.stem,
        shape="file",
        ref=absolute_path.resolve().as_uri(),
        path=str(relative_path),
        source=_source_record(
            root_relative_path=relative_path,
            absolute_path=absolute_path,
            form="local",
            shape="file",
        ),
        meta=_load_meta(absolute_path),
    )


def _source_record(
    *,
    root_relative_path: Path,
    absolute_path: Path,
    form: Literal["local", "inline", "remote"],
    shape: Literal["file", "dir"],
) -> PreparedSource:
    fingerprint = _dir_fingerprint(absolute_path) if shape == "dir" else hashlib.sha256(absolute_path.read_bytes()).hexdigest()
    return PreparedSource(
        form=form,
        path=str(root_relative_path),
        updated_at=_updated_at(absolute_path, shape=shape),
        fingerprint=fingerprint,
    )


def _ensure_no_conflicts(entries: tuple[PreparedEntry, ...]) -> None:
    seen: dict[tuple[str, str], str] = {}
    for entry in entries:
        key = (entry.kind, entry.name)
        existing = seen.get(key)
        if existing is not None and existing != entry.ref:
            raise ValueError(
                f"conflicting entries in one scope: kind={entry.kind} name={entry.name}"
            )
        seen[key] = entry.ref


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


def _content_fingerprint(
    toolang_root: Path,
    entry: PreparedEntry,
    materialized_files: Mapping[str, bytes],
) -> str:
    if entry.source.form == "local":
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


def _validate_local_kind(scope: PreparedScope, kind: EntryKind) -> None:
    if kind not in MANAGED_KINDS:
        raise ValueError(f"unsupported kind: {kind}")
    if scope == "global" and kind in JOB_KINDS:
        raise ValueError(f"global scope does not support kind: {kind}")


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
    scope: PreparedScope,
    kind: EntryKind,
    name: str,
    ref: str,
) -> None:
    for entry in list_entries(toolang_root, agent_name, scope=scope, kinds={kind}):
        if entry.name == name and entry.ref != ref:
            raise ValueError(f"conflicting entries in one scope: kind={kind} name={name}")


def _local_entry_file_path(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: PreparedScope,
    kind: EntryKind,
    name: str,
) -> Path:
    return toolang_root / _relative_entry_file_path(agent_name, scope=scope, kind=kind, name=name)


def _relative_entry_file_path(
    agent_name: str,
    *,
    scope: PreparedScope,
    kind: EntryKind,
    name: str,
) -> Path:
    definition_root = _relative_definition_root(agent_name, scope=scope, kind=kind, name=name)
    if kind == "skill":
        return definition_root / "SKILL.md"
    return definition_root.with_suffix(".md")


def _relative_definition_root(
    agent_name: str,
    *,
    scope: PreparedScope,
    kind: EntryKind,
    name: str,
) -> Path:
    prefix = Path() if scope == "global" else Path("agents") / agent_name
    return prefix / DIR_NAME_BY_KIND[kind] / name


def _local_parts(relative_path: Path, *, agent_name: str, scope: PreparedScope) -> tuple[str, ...]:
    if scope == "agent" and relative_path.parts[:2] == ("agents", agent_name):
        return relative_path.parts[2:]
    return relative_path.parts


def _collect_remote_entries(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: PreparedScope | None = None,
    kinds: set[EntryKind] | None = None,
) -> tuple[tuple[PreparedEntry, ...], dict[str, bytes]]:
    scopes = ("global", "agent") if scope is None else (scope,)
    entries: list[PreparedEntry] = []
    files: dict[str, bytes] = {}
    for item_scope in scopes:
        config_path = _config_path(toolang_root, agent_name, scope=item_scope)
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
                ref = _config_ref(item)
                entry, entry_files = _remote_entry_from_ref(
                    toolang_root,
                    agent_name,
                    scope=item_scope,
                    kind=kind,
                    ref=ref,
                    name=name,
                    relative_config_path=relative_config_path,
                    config_path=config_path,
                )
                entries.append(entry)
                files.update(entry_files)
    return tuple(sorted(entries, key=_entry_sort_key)), files


def _remote_entry_from_ref(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: PreparedScope,
    kind: EntryKind,
    ref: str,
    name: str,
    relative_config_path: Path,
    config_path: Path,
) -> tuple[PreparedEntry, dict[str, bytes]]:
    canonical_ref = _canonicalize_remote_ref(kind, ref)
    relative_entry_path = _relative_remote_entry_path(agent_name, scope=scope, kind=kind, name=name)
    content = _remote_materialized_content(kind=kind, name=name, ref=canonical_ref)
    return (
        PreparedEntry(
            kind=kind,
            name=name,
            shape="dir" if kind == "skill" else "file",
            ref=canonical_ref,
            path=str(relative_entry_path),
            source=_source_record(
                root_relative_path=relative_config_path,
                absolute_path=config_path,
                form="remote",
                shape="file",
            ),
            meta=_load_meta_text(content.decode("utf-8")),
        ),
        {str(relative_entry_path): content},
    )


def _relative_remote_entry_path(
    agent_name: str,
    *,
    scope: PreparedScope,
    kind: EntryKind,
    name: str,
) -> Path:
    prefix = Path(".prepared") if scope == "global" else Path("agents") / agent_name / ".prepared"
    root = prefix / "remote" / DIR_NAME_BY_KIND[kind] / name
    if kind == "skill":
        return root / "SKILL.md"
    return root.with_suffix(".md")


def _remote_materialized_content(
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


def _canonicalize_remote_ref(kind: EntryKind, ref: str) -> str:
    text = ref.strip()
    if "://" in text:
        return text
    parts = text.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"invalid remote ref: {ref}")
    owner, name = parts
    if kind == "skill":
        return f"github://{owner}/agent-skills/skills/{name}"
    if kind == "service":
        return f"github://{owner}/agent-services/services/{name}.md"
    if kind == "prompt":
        return f"github://{owner}/agent-prompts/prompts/{name}.md"
    if kind == "psyche":
        return f"github://{owner}/agent-psyches/psyches/{name}.md"
    raise ValueError(f"unsupported remote kind: {kind}")


def _remote_name(kind: EntryKind, ref: str) -> str:
    parsed = urlparse(ref)
    path = parsed.path.rstrip("/")
    if not path:
        raise ValueError(f"invalid remote ref: {ref}")
    name = Path(path).name
    if kind == "skill":
        return name
    if "@" in name:
        name = name.split("@", 1)[0]
    return Path(name).stem


def _config_path(toolang_root: Path, agent_name: str, *, scope: PreparedScope) -> Path:
    if scope == "global":
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
    scope: PreparedScope,
    kind: EntryKind,
    name: str,
) -> bool:
    if kind in JOB_KINDS:
        return False
    config_path = _config_path(toolang_root, agent_name, scope=scope)
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
