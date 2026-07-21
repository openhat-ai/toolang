"""Resolved and prepared cap state plus immutable runtime agent state."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
from hashlib import sha256
import io
import json
from pathlib import Path
import tarfile
from typing import Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import frontmatter

from toolang.catalog import cap as cap_catalog
from toolang.state.source import AuthoredFile, AuthoredSource, read_authored_source
from ..common.immutable import freeze_mapping, mutable_data
from ..common.progress import ProgressSink, emit_progress
from ..lang.ast import CapDecl, Program, to_data
from toolang.common.selectors import (
    Selector,
    filter_value_matches,
    parse_selector,
    split_selector_list,
    selector_identity_matches,
)
from toolang.common.github import (
    GitHubRef,
    github_raw_url,
    parse_github_ref,
    parse_github_url,
)

from .types import (
    EntryForm,
    EntryKind,
    EntryOrigin,
    EntryScope,
    EntryShape,
    PreparedVisibility,
    SourceForm,
    SourceOrigin,
    Visibility,
)

CAP_KINDS: tuple[EntryKind, ...] = cap_catalog.CAP_KINDS
EMBEDDED_CAP_KINDS = frozenset({"psyche", "service", "prompt"})
FILE_BACKED_KINDS = frozenset({"psyche", "service", "prompt"})
DIR_NAME_BY_KIND: dict[EntryKind, str] = cap_catalog.CAP_DIR_BY_KIND
KIND_BY_DIR_NAME: dict[str, EntryKind] = cap_catalog.CAP_KIND_BY_DIR
REMOTE_CAP_MATERIALIZE_WORKERS = 4
_AGENT_STATE_VERSION_DOMAIN = b"toolang-agent-state-v1\0"


@dataclass(frozen=True, slots=True)
class ResolvedFile:
    """One file fixed by a remote cap resolution."""

    path: str
    size: int
    sha256: str

    def to_data(self) -> dict[str, object]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> ResolvedFile:
        size = data["size"]
        if not isinstance(size, int) or isinstance(size, bool):
            raise TypeError("resolved file size must be an integer")
        return cls(
            path=str(data["path"]),
            size=size,
            sha256=str(data["sha256"]),
        )


@dataclass(frozen=True, slots=True)
class CapResolution:
    """Persisted resolution of one cited or wired cap reference."""

    kind: EntryKind
    name: str
    form: SourceForm
    authored_ref: str
    resolved_ref: str
    definition: str
    materialized: str
    content_hash: str
    files: tuple[ResolvedFile, ...]
    line: int | None = None

    def to_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "kind": self.kind,
            "name": self.name,
            "form": self.form,
            "authored_ref": self.authored_ref,
            "resolved_ref": self.resolved_ref,
            "definition": self.definition,
            "materialized": self.materialized,
            "content_hash": self.content_hash,
            "files": [file.to_data() for file in self.files],
        }
        if self.line is not None:
            data["line"] = self.line
        return data

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> CapResolution:
        raw_files = data["files"]
        if not isinstance(raw_files, list):
            raise TypeError("resolved cap files must be a list")
        line = data.get("line")
        return cls(
            kind=cast(EntryKind, str(data["kind"])),
            name=str(data["name"]),
            form=cast(SourceForm, str(data["form"])),
            authored_ref=str(data["authored_ref"]),
            resolved_ref=str(data["resolved_ref"]),
            definition=str(data["definition"]),
            materialized=str(data["materialized"]),
            content_hash=str(data["content_hash"]),
            files=tuple(
                ResolvedFile.from_data(cast(dict[str, object], file))
                for file in raw_files
                if isinstance(file, dict)
            ),
            line=line if isinstance(line, int) and not isinstance(line, bool) else None,
        )


@dataclass(frozen=True, slots=True)
class CapSource:
    """Authored provenance retained by one prepared cap."""

    origin: SourceOrigin
    form: SourceForm
    path: str
    updated_at: str
    fingerprint: str
    authored_ref: str | None = None
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
        if self.authored_ref is not None:
            data["authored_ref"] = self.authored_ref
        return data

    @classmethod
    def from_data(cls, data: dict[str, object]) -> CapSource:
        raw_line = data.get("line")
        return cls(
            origin=cast(SourceOrigin, str(data["origin"])),
            form=cast(SourceForm, str(data["form"])),
            path=str(data["path"]),
            updated_at=str(data["updated_at"]),
            fingerprint=str(data["fingerprint"]),
            authored_ref=(
                str(data["authored_ref"])
                if data.get("authored_ref") is not None
                else None
            ),
            line=raw_line if isinstance(raw_line, int) else None,
        )


@dataclass(frozen=True, slots=True)
class PreparedCap:
    """One cap backed by an immutable prepared filesystem path."""

    kind: EntryKind
    name: str
    shape: EntryShape
    ref: str
    path: str
    source: CapSource
    meta: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "meta", freeze_mapping(self.meta))

    @property
    def visibility(self) -> PreparedVisibility:
        if self.source.form in {"inline", "ref"}:
            return "private"
        return "private" if self.source.path.startswith("agents/") else "shared"

    def to_data(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "shape": self.shape,
            "ref": self.ref,
            "path": self.path,
            "source": self.source.to_data(),
            "meta": mutable_data(self.meta),
        }

    def read_text(self) -> str:
        """Read this cap from its immutable prepared file."""

        return Path(self.path).read_text(encoding="utf-8")

    def read_content(self) -> str:
        """Read the cap body lazily from its immutable prepared file."""

        return frontmatter.loads(self.read_text()).content.strip()

    def to_snapshot(self) -> dict[str, object]:
        return self.to_data()

    @classmethod
    def from_data(cls, data: dict[str, object]) -> PreparedCap:
        return cls(
            kind=cast(EntryKind, str(data["kind"])),
            name=str(data["name"]),
            shape=cast(EntryShape, str(data["shape"])),
            ref=str(data["ref"]),
            path=str(data["path"]),
            source=CapSource.from_data(cast(dict[str, object], data["source"])),
            meta=dict(cast(dict[str, object], data.get("meta", {}))),
        )


@dataclass(frozen=True, slots=True)
class AgentState:
    """Program and effective prepared caps fixed for one top-level run."""

    version: bytes
    root_version: bytes
    home_version: bytes
    toolang_version: str
    root_config: Mapping[str, object]
    home_config: Mapping[str, object]
    config: Mapping[str, object]
    program: Program
    caps: tuple[PreparedCap, ...]
    loaded_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "root_config", freeze_mapping(self.root_config))
        object.__setattr__(self, "home_config", freeze_mapping(self.home_config))
        object.__setattr__(self, "config", freeze_mapping(self.config))

    @property
    def fingerprint(self) -> str:
        return self.version.hex()

    @property
    def updated_at(self) -> str:
        return self.loaded_at

    def to_snapshot(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "version": self.version.hex(),
            "root_version": self.root_version.hex(),
            "home_version": self.home_version.hex(),
            "toolang_version": self.toolang_version,
            "updated_at": self.loaded_at,
            "loaded_at": self.loaded_at,
            "program": to_data(self.program),
            "caps": [cap.path for cap in self.caps],
        }


def compose_agent_state(
    *,
    root_version: bytes,
    home_version: bytes,
    toolang_version: str,
    root_config: Mapping[str, object],
    home_config: Mapping[str, object],
    program: Program,
    root_caps: tuple[PreparedCap, ...],
    home_caps: tuple[PreparedCap, ...],
    loaded_at: str,
) -> AgentState:
    """Compose runtime state without retaining prepared cache layers."""

    return AgentState(
        version=agent_state_version(root_version, home_version),
        root_version=root_version,
        home_version=home_version,
        toolang_version=toolang_version,
        root_config=root_config,
        home_config=home_config,
        config=_merge_config(root_config, home_config),
        program=program,
        caps=effective_caps(root_caps, home_caps),
        loaded_at=loaded_at,
    )


def _merge_config(
    base: Mapping[str, object], override: Mapping[str, object]
) -> dict[str, object]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_config(
                cast(Mapping[str, object], current),
                cast(Mapping[str, object], value),
            )
        else:
            merged[key] = value
    return merged


def agent_state_version(root_version: bytes, home_version: bytes) -> bytes:
    """Return the version of one exact root and home prepared pair."""

    _require_sha256(root_version, name="root version")
    _require_sha256(home_version, name="home version")
    digest = sha256()
    digest.update(_AGENT_STATE_VERSION_DOMAIN)
    digest.update(root_version)
    digest.update(home_version)
    return digest.digest()


def effective_caps(
    root: tuple[PreparedCap, ...],
    home: tuple[PreparedCap, ...],
) -> tuple[PreparedCap, ...]:
    """Overlay private prepared caps over shared caps."""

    effective: dict[tuple[str, str], PreparedCap] = {}
    for cap in (*root, *home):
        effective[(cap.kind, cap.name)] = cap
    return tuple(
        sorted(
            effective.values(),
            key=lambda cap: (cap.kind, cap.name, cap.ref),
        )
    )


def _require_sha256(value: bytes, *, name: str) -> None:
    if len(value) != sha256().digest_size:
        raise ValueError(f"{name} must contain 32 bytes")


def resolve_remote_ref(
    kind: EntryKind,
    ref: str,
    *,
    progress: ProgressSink | None = None,
) -> str:
    """Resolve one remote cap ref or shorthand to a canonical ref."""

    text = ref.strip()
    emit_progress(
        progress,
        id=f"cap.resolve:{kind}:{text}",
        phase="cap.resolve",
        label=f"Resolve {kind}",
        status="running",
        detail=text,
    )
    try:
        if "://" in text:
            canonical = canonicalize_remote_ref(kind, text)
            if canonical.startswith("github://") and not _github_remote_exists(
                kind, canonical
            ):
                raise ValueError(
                    f"remote {kind} not found or missing entry file: {ref}"
                )
        else:
            candidates = _remote_ref_candidates(kind, text)
            canonical = next(
                (
                    candidate
                    for candidate in candidates
                    if _github_remote_exists(kind, candidate)
                ),
                "",
            )
            if not canonical:
                message = (
                    f"invalid remote ref: {ref}"
                    if not candidates
                    else f"could not resolve remote {kind} shorthand: {ref}"
                )
                raise ValueError(message)
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
        detail=canonical,
    )
    return canonical


def canonicalize_remote_ref(kind: EntryKind, ref: str) -> str:
    """Canonicalize one explicit or shorthand remote cap ref."""

    text = ref.strip()
    if "://" in text:
        github_ref = parse_github_url(text)
        if github_ref is None and text.startswith("github://"):
            github_ref = parse_github_ref(text)
        if github_ref is not None:
            if kind == "skill" and Path(github_ref.path).name == "SKILL.md":
                github_ref = GitHubRef(
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


def remote_entry_name(kind: EntryKind, ref: str) -> str:
    """Return the default authored name for one remote cap ref."""

    canonical_ref = canonicalize_remote_ref(kind, ref)
    if canonical_ref.startswith("github://"):
        path = parse_github_ref(canonical_ref).path.rstrip("/")
        if not path:
            raise ValueError(f"invalid remote ref: {ref}")
        return Path(path).name if kind == "skill" else Path(path).stem
    parsed = urlparse(canonical_ref)
    path = parsed.path.rstrip("/")
    if not path:
        raise ValueError(f"invalid remote ref: {ref}")
    name = Path(path).name.split("@", 1)[0]
    return name if kind == "skill" else Path(name).stem


def _remote_ref_candidates(kind: EntryKind, ref: str) -> tuple[str, ...]:
    slash_count = ref.count("/")
    if slash_count == 2:
        owner, repo, name = ref.split("/", 2)
        if not owner or not repo or not name:
            return ()
        return _remote_ref_candidates_for_repo(kind, owner, repo, name)
    if slash_count != 1:
        return ()
    owner, name = ref.split("/", 1)
    if not owner or not name:
        return ()
    repositories: dict[EntryKind, tuple[tuple[str, str], ...]] = {
        "skill": (
            ("agents", f"skills/{name}"),
            ("agent-skills", name),
            ("agent-skills", f"skills/{name}"),
            ("skills", name),
            ("skills", f"skills/{name}"),
        ),
        "service": (
            ("agents", f"services/{name}.md"),
            ("agent-services", f"{name}.md"),
            ("services", f"{name}.md"),
        ),
        "prompt": (
            ("agents", f"prompts/{name}.md"),
            ("agent-prompts", f"{name}.md"),
            ("prompts", f"{name}.md"),
        ),
        "psyche": (
            ("agents", f"psyches/{name}.md"),
            ("agent-psyches", f"{name}.md"),
            ("psyches", f"{name}.md"),
        ),
    }
    return tuple(
        _github_remote_ref_with_default_branch(owner, repo, path)
        for repo, path in repositories[kind]
    )


def _remote_ref_candidates_for_repo(
    kind: EntryKind,
    owner: str,
    repo: str,
    name: str,
) -> tuple[str, ...]:
    return tuple(
        _github_remote_ref_with_default_branch(owner, repo, path)
        for path in _remote_path_candidates_for_repo(kind, repo, name)
    )


def _remote_path_candidates_for_repo(
    kind: EntryKind,
    repo: str,
    name: str,
) -> tuple[str, ...]:
    if kind == "skill":
        if repo in {"agent-skills", "skills"}:
            return (name, f"skills/{name}")
        return (f"skills/{name}", name)
    directory = DIR_NAME_BY_KIND[kind]
    if repo in {f"agent-{directory}", directory}:
        return (f"{name}.md",)
    return (f"{directory}/{name}.md", f"{name}.md")


def _github_remote_ref_with_default_branch(owner: str, repo: str, path: str) -> str:
    try:
        rev = _github_repo_default_branch(owner, repo)
    except ValueError:
        rev = "main"
    return GitHubRef(owner=owner, repo=repo, path=path, rev=rev).render()


def _github_remote_exists(kind: EntryKind, ref: str) -> bool:
    github_ref = parse_github_ref(ref)
    probe_ref = github_ref
    if kind == "skill":
        probe_ref = GitHubRef(
            owner=github_ref.owner,
            repo=github_ref.repo,
            path=str(Path(github_ref.path) / "SKILL.md"),
            rev=github_ref.rev,
        )
    request = Request(
        github_raw_url(probe_ref),
        method="HEAD",
        headers={"User-Agent": "toolang/0.1"},
    )
    try:
        with urlopen(request, timeout=30):
            return True
    except (HTTPError, URLError):
        return False


@lru_cache
def _github_repo_default_branch(owner: str, repo: str) -> str:
    api_url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    )
    request = Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "toolang/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"could not resolve GitHub default branch: {owner}/{repo}"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("default_branch"), str):
        raise ValueError(f"unexpected GitHub repository response: {owner}/{repo}")
    return data["default_branch"]


@dataclass(frozen=True, slots=True)
class _RemoteEntryRequest:
    visibility: PreparedVisibility
    kind: EntryKind
    ref: str
    name: str | None
    relative_config_path: Path
    source_fingerprint: str
    source_mtime_ns: int
    form: Literal["wired", "ref"]
    source_line: int | None = None


@dataclass(frozen=True, slots=True)
class _CachedRemoteEntry:
    ref: str
    files: tuple[tuple[str, bytes], ...]


_RemoteEntryCacheKey = tuple[
    PreparedVisibility,
    EntryKind,
    str,
    str | None,
    str,
]
_RemoteEntryCache = Mapping[_RemoteEntryCacheKey, _CachedRemoteEntry]


def list_entries(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility | None = None,
    kinds: set[EntryKind] | None = None,
) -> tuple[PreparedCap, ...]:
    """List effective cap entries projected from authored authored state."""

    authored = read_authored_source(toolang_root, agent_name)
    entries, _ = _collect_visibility_entries_with_files(
        authored, visibility=visibility, kinds=kinds
    )
    return entries


def entry_visibility(entry: PreparedCap, *, agent_name: str) -> Visibility:
    """Return the external visibility for one prepared entry."""

    del agent_name
    return entry.visibility


def entry_origin(entry: PreparedCap) -> EntryOrigin:
    """Return where one prepared entry's content originates."""

    return entry.source.origin


def entry_form(entry: PreparedCap) -> EntryForm:
    """Return how one prepared entry is authored or attached."""

    return entry.source.form


def entry_scope(entry: PreparedCap, *, agent_name: str) -> EntryScope:
    """Return where one prepared entry is available."""

    if entry.source.form in {"inline", "ref"}:
        return "here"
    if entry_visibility(entry, agent_name=agent_name) == "shared":
        return "root"
    return "home"


def entry_ref(entry: PreparedCap, *, agent_name: str) -> str:
    """Return the canonical external ref for one prepared entry."""

    origin = entry_origin(entry)
    if origin == "remote":
        return entry.ref
    if entry.source.form == "inline":
        return f"inline://{DIR_NAME_BY_KIND[entry.kind]}/{entry.name}"
    visibility = entry_visibility(entry, agent_name=agent_name)
    return f"{'root' if visibility == 'shared' else 'home'}://{DIR_NAME_BY_KIND[entry.kind]}/{entry.name}"


def entry_definition_file(entry: PreparedCap) -> str:
    """Return the authored file that defines or links one prepared entry."""

    return entry.source.path


def entry_line(entry: PreparedCap) -> int | None:
    """Return the authored source line for one prepared entry when known."""

    return entry.source.line


def split_cap_selectors(items: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Split repeated and CSV cap selector inputs."""

    return split_selector_list(items)


def cap_entry_matches_selector(
    entry: PreparedCap,
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
    entries: tuple[PreparedCap, ...],
    selectors: list[str] | tuple[str, ...] | None,
    *,
    agent_name: str,
    implicit_kind: EntryKind | None = None,
) -> tuple[PreparedCap, ...]:
    """Return entries selected by a selector list."""

    parsed = tuple(
        parse_selector(raw, domain="cap", implicit_family=implicit_kind)
        for raw in split_cap_selectors(selectors)
    )
    if not parsed:
        return entries
    selected: list[PreparedCap] = []
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
    entry: PreparedCap,
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
    authored: AuthoredSource,
    *,
    visibility: PreparedVisibility | None = None,
    kinds: set[EntryKind] | None = None,
) -> tuple[PreparedCap, ...]:
    """Collect local prepared entries from authored authored files."""

    entries: dict[str, PreparedCap] = {}
    for item in authored.files:
        entry = _local_entry_from_file(authored, item)
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
    authored: AuthoredSource,
    *,
    visibility: PreparedVisibility | None = None,
    kinds: set[EntryKind] | None = None,
    materialize_remote: bool = False,
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
) -> tuple[tuple[PreparedCap, ...], dict[str, bytes]]:
    local_entries = collect_local_entries(authored, visibility=visibility, kinds=kinds)
    remote_entries, files = _collect_remote_entries(
        authored,
        visibility=visibility,
        kinds=kinds,
        materialize=materialize_remote,
        remote_cache=remote_cache,
        progress=progress,
    )
    embedded_entries, embedded_files = _collect_program_embedded_entries(
        authored,
        visibility=visibility,
        kinds=kinds,
        materialize=materialize_remote,
    )
    use_entries, use_files = _collect_program_use_entries(
        authored,
        visibility=visibility,
        kinds=kinds,
        materialize=materialize_remote,
        remote_cache=remote_cache,
        progress=progress,
    )
    files.update(embedded_files)
    files.update(use_files)
    entries = _dedupe_entries(
        (*local_entries, *remote_entries, *embedded_entries, *use_entries)
    )
    return entries, files


def materialize_visibility(
    authored: AuthoredSource,
    *,
    visibility: PreparedVisibility,
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
) -> tuple[tuple[PreparedCap, ...], dict[str, bytes]]:
    """Build prepared cap entries and materialized files for one visibility."""

    emit_progress(
        progress,
        id=f"prepare.visibility:{visibility}",
        phase="prepare.visibility",
        label=f"Prepare {visibility} caps",
        status="running",
        detail=authored.agent_name,
    )
    entries, files = _collect_visibility_entries_with_files(
        authored,
        visibility=visibility,
        materialize_remote=True,
        remote_cache=remote_cache,
        progress=progress,
    )
    _ensure_no_conflicts(entries)
    emit_progress(
        progress,
        id=f"prepare.visibility:{visibility}",
        phase="prepare.visibility",
        label=f"Prepare {visibility} caps",
        status="ok",
        detail=f"{len(entries)} entries",
    )
    return entries, files


def prepared_remote_cache(
    authored: AuthoredSource,
    *,
    visibility: PreparedVisibility,
    entries: tuple[PreparedCap, ...],
) -> _RemoteEntryCache:
    """Build reusable remote inputs from one immutable prepared version."""

    cache: _RemoteEntryCache = {}
    for entry in entries:
        if entry.source.origin != "remote":
            continue
        files = _cache_entry_files(authored.toolang_root, entry)
        authored_ref = entry.source.authored_ref
        if files is None or authored_ref is None:
            continue
        key = _remote_entry_cache_key(
            visibility=visibility,
            kind=entry.kind,
            form=entry.source.form,
            name=entry.name if entry.source.form == "wired" else None,
            authored_ref=authored_ref,
        )
        cache[key] = _CachedRemoteEntry(
            ref=entry.ref,
            files=files,
        )
    return cache


def _remote_entry_cache_key(
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    form: SourceForm,
    name: str | None,
    authored_ref: str,
) -> _RemoteEntryCacheKey:
    return (
        visibility,
        kind,
        form,
        name if form == "wired" else None,
        authored_ref,
    )


def _cache_entry_files(
    toolang_root: Path,
    entry: PreparedCap,
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
        authored_ref=request.ref,
    )
    return remote_cache.get(key)


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


def authored_entries_snapshot(
    authored: AuthoredSource,
) -> dict[str, object]:
    """Return a JSON-friendly authored definitions snapshot."""

    shared_entries, _ = _collect_visibility_entries_with_files(
        authored, visibility="shared"
    )
    private_entries, _ = _collect_visibility_entries_with_files(
        authored, visibility="private"
    )
    return {
        "program_source": authored.program_path,
        "config_paths": list(authored.config_paths),
        "shared_entries": [entry.to_snapshot() for entry in shared_entries],
        "private_entries": [entry.to_snapshot() for entry in private_entries],
    }


def _local_entry_from_file(
    authored: AuthoredSource,
    item: AuthoredFile,
) -> PreparedCap | None:
    if item.category != "cap":
        return None
    visibility: PreparedVisibility = "shared" if item.origin == "root" else "private"
    relative_path = Path(item.relative_path)
    local_parts = _local_parts(
        relative_path, agent_name=authored.agent_name, visibility=visibility
    )
    if len(local_parts) < 2:
        return None
    directory_name = local_parts[0]
    kind = KIND_BY_DIR_NAME.get(directory_name)
    if kind is None:
        return None
    if kind == "skill":
        if tuple(local_parts[2:]) != ("SKILL.md",):
            return None
        return _skill_entry(authored, item, visibility=visibility, name=local_parts[1])
    if kind in FILE_BACKED_KINDS and len(local_parts) == 2:
        return _file_entry(item, kind=kind)
    return None


def _skill_entry(
    authored: AuthoredSource,
    definition: AuthoredFile,
    *,
    visibility: PreparedVisibility,
    name: str,
) -> PreparedCap:
    prefix = Path() if visibility == "shared" else Path("agents") / authored.agent_name
    root_relative_dir = prefix / DIR_NAME_BY_KIND["skill"] / name
    root_relative_file = root_relative_dir / "SKILL.md"
    return PreparedCap(
        kind="skill",
        name=name,
        shape="dir",
        ref=_local_cap_ref(visibility, "skill", name),
        path=str(root_relative_file),
        source=_snapshot_source_record(
            authored,
            root_relative_path=root_relative_dir,
        ),
        meta=_load_meta_text(definition.read_text()),
    )


def _file_entry(
    item: AuthoredFile,
    *,
    kind: EntryKind,
) -> PreparedCap:
    relative_path = Path(item.relative_path)
    return PreparedCap(
        kind=kind,
        name=relative_path.stem,
        shape="file",
        ref=_local_cap_ref(
            _visibility_from_relative_path(relative_path), kind, relative_path.stem
        ),
        path=str(relative_path),
        source=_snapshot_file_source_record(
            item,
            root_relative_path=relative_path,
        ),
        meta=_load_meta_text(item.read_text()),
    )


def _snapshot_source_record(
    authored: AuthoredSource,
    *,
    root_relative_path: Path,
) -> CapSource:
    files = tuple(
        item
        for item in authored.files
        if Path(item.relative_path).is_relative_to(root_relative_path)
    )
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda candidate: candidate.relative_path):
        relative_path = Path(item.relative_path).relative_to(root_relative_path)
        digest.update(str(relative_path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.digest.encode("utf-8"))
        digest.update(b"\n")
    latest_mtime_ns = max(item.mtime_ns for item in files)
    return CapSource(
        origin="local",
        form="file",
        path=str(root_relative_path),
        updated_at=_mtime_text(latest_mtime_ns),
        fingerprint=digest.hexdigest(),
    )


def _snapshot_file_source_record(
    item: AuthoredFile,
    *,
    root_relative_path: Path,
) -> CapSource:
    return CapSource(
        origin="local",
        form="file",
        path=str(root_relative_path),
        updated_at=_mtime_text(item.mtime_ns),
        fingerprint=item.digest,
    )


def _mtime_text(mtime_ns: int) -> str:
    return datetime.fromtimestamp(
        mtime_ns / 1_000_000_000,
        tz=timezone.utc,
    ).isoformat()


def _source_record(
    *,
    root_relative_path: Path,
    absolute_path: Path,
    origin: EntryOrigin,
    form: EntryForm,
    shape: Literal["file", "dir"],
    line: int | None = None,
) -> CapSource:
    fingerprint = (
        _dir_fingerprint(absolute_path)
        if shape == "dir"
        else _file_fingerprint(absolute_path)
    )
    return CapSource(
        origin=origin,
        form=form,
        path=str(root_relative_path),
        updated_at=_updated_at(absolute_path, shape=shape),
        fingerprint=fingerprint,
        line=line,
    )


def _file_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ensure_no_conflicts(entries: tuple[PreparedCap, ...]) -> None:
    seen: dict[tuple[str, str], str] = {}
    for entry in entries:
        key = (entry.kind, entry.name)
        existing = seen.get(key)
        if existing is not None and existing != entry.ref:
            raise ValueError(
                f"conflicting entries in one visibility: kind={entry.kind} name={entry.name}"
            )
        seen[key] = entry.ref


def _dedupe_entries(entries: tuple[PreparedCap, ...]) -> tuple[PreparedCap, ...]:
    by_ref: dict[str, PreparedCap] = {}
    for entry in sorted(entries, key=_entry_sort_key):
        by_ref.setdefault(entry.ref, entry)
    return tuple(sorted(by_ref.values(), key=_entry_sort_key))


def _dir_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(item for item in path.rglob("*") if item.is_file())
    for item in files:
        digest.update(str(item.relative_to(path)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(item.read_bytes()).hexdigest().encode("utf-8"))
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


def _entry_sort_key(entry: PreparedCap) -> tuple[str, str, str]:
    return (entry.kind, entry.name, entry.ref)


def _visibility_from_relative_path(relative_path: Path) -> PreparedVisibility:
    return "private" if relative_path.parts[:1] == ("agents",) else "shared"


def _local_cap_ref(
    visibility: PreparedVisibility,
    kind: EntryKind,
    name: str,
) -> str:
    scope = "root" if visibility == "shared" else "home"
    return f"{scope}://{DIR_NAME_BY_KIND[kind]}/{name}"


def _local_parts(
    relative_path: Path, *, agent_name: str, visibility: PreparedVisibility
) -> tuple[str, ...]:
    if visibility == "private" and relative_path.parts[:2] == ("agents", agent_name):
        return relative_path.parts[2:]
    return relative_path.parts


def _collect_remote_entries(
    authored: AuthoredSource,
    *,
    visibility: PreparedVisibility | None = None,
    kinds: set[EntryKind] | None = None,
    materialize: bool = False,
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
) -> tuple[tuple[PreparedCap, ...], dict[str, bytes]]:
    requests = _collect_remote_entry_requests(
        authored,
        visibility=visibility,
        kinds=kinds,
    )
    return _materialize_remote_entry_requests(
        requests,
        materialize=materialize,
        remote_cache=remote_cache,
        progress=progress,
    )


def _collect_remote_entry_requests(
    authored: AuthoredSource,
    *,
    visibility: PreparedVisibility | None,
    kinds: set[EntryKind] | None,
) -> tuple[_RemoteEntryRequest, ...]:
    visibilities = ("shared", "private") if visibility is None else (visibility,)
    requests: list[_RemoteEntryRequest] = []
    for item_visibility in visibilities:
        config_origin = "root" if item_visibility == "shared" else "agent"
        config_file = next(
            (
                item
                for item in authored.files
                if item.category == "config" and item.origin == config_origin
            ),
            None,
        )
        if config_file is None:
            continue
        for entry in cap_catalog.WiredCaps(config_file.path).parse(
            config_file.read_text(), kinds=kinds
        ):
            requests.append(
                _RemoteEntryRequest(
                    visibility=item_visibility,
                    kind=entry.kind,
                    ref=entry.ref,
                    name=entry.name,
                    relative_config_path=Path(config_file.relative_path),
                    source_fingerprint=config_file.digest,
                    source_mtime_ns=config_file.mtime_ns,
                    form="wired",
                )
            )
    return tuple(requests)


def _collect_program_use_entries(
    authored: AuthoredSource,
    *,
    visibility: PreparedVisibility | None = None,
    kinds: set[EntryKind] | None = None,
    materialize: bool = False,
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
) -> tuple[tuple[PreparedCap, ...], dict[str, bytes]]:
    if visibility == "shared" or authored.program_path is None:
        return (), {}
    program_source = authored.load_program()
    program = program_source.parse()
    relative_program_path = Path(program_source.source_path)
    program_file = next(item for item in authored.files if item.category == "program")
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
                source_fingerprint=program_file.digest,
                source_mtime_ns=program_file.mtime_ns,
                form="ref",
                source_line=use.span.line,
            )
        )
    return _materialize_remote_entry_requests(
        tuple(requests),
        materialize=materialize,
        remote_cache=remote_cache,
        progress=progress,
    )


def _collect_program_embedded_entries(
    authored: AuthoredSource,
    *,
    visibility: PreparedVisibility | None = None,
    kinds: set[EntryKind] | None = None,
    materialize: bool = False,
) -> tuple[tuple[PreparedCap, ...], dict[str, bytes]]:
    del materialize
    if visibility == "shared" or authored.program_path is None:
        return (), {}
    program_source = authored.load_program()
    program = program_source.parse()
    relative_program_path = Path(program_source.source_path)
    program_path = authored.toolang_root / relative_program_path
    entries: list[PreparedCap] = []
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
    *,
    kind: EntryKind,
    cap: CapDecl,
    relative_program_path: Path,
    program_path: Path,
    source_line: int,
) -> tuple[PreparedCap, dict[str, bytes]]:
    relative_entry_path = _relative_embedded_entry_path(kind=kind, name=cap.name)
    content = _embedded_materialized_content(cap)
    return (
        PreparedCap(
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
    *,
    kind: EntryKind,
    name: str,
) -> Path:
    return Path("inline") / DIR_NAME_BY_KIND[kind] / f"{name}.md"


def _embedded_materialized_content(cap: CapDecl) -> bytes:
    if not cap.meta:
        return cap.body.encode("utf-8")
    post = frontmatter.Post(cap.body, **dict(cap.meta))
    return frontmatter.dumps(post).encode("utf-8")


def _remote_entry_from_ref(
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    ref: str,
    name: str | None,
    relative_config_path: Path,
    source_fingerprint: str,
    source_mtime_ns: int,
    form: Literal["wired", "ref"],
    source_line: int | None = None,
    materialize: bool,
    remote_cache: _RemoteEntryCache | None = None,
    progress: ProgressSink | None = None,
) -> tuple[PreparedCap, dict[str, bytes]]:
    request = _RemoteEntryRequest(
        visibility=visibility,
        kind=kind,
        ref=ref,
        name=name,
        relative_config_path=relative_config_path,
        source_fingerprint=source_fingerprint,
        source_mtime_ns=source_mtime_ns,
        form=form,
        source_line=source_line,
    )
    cached = _cached_remote_entry(remote_cache, request)
    if cached is not None:
        canonical_ref = cached.ref
        _emit_cached_remote_progress(request, canonical_ref, progress=progress)
    elif materialize and "://" not in ref:
        canonical_ref = resolve_remote_ref(kind, ref, progress=progress)
    else:
        canonical_ref = canonicalize_remote_ref(kind, ref)
    if name is None:
        name = remote_entry_name(kind, canonical_ref)
    relative_entry_path = _relative_remote_entry_path(
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
        entry = PreparedCap(
            kind=kind,
            name=name,
            shape="dir" if kind == "skill" else "file",
            ref=canonical_ref,
            path=str(relative_entry_path),
            source=CapSource(
                origin="remote",
                form=form,
                path=str(relative_config_path),
                updated_at=_mtime_text(source_mtime_ns),
                fingerprint=source_fingerprint,
                authored_ref=ref,
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
    requests: tuple[_RemoteEntryRequest, ...],
    *,
    materialize: bool,
    remote_cache: _RemoteEntryCache | None,
    progress: ProgressSink | None,
) -> tuple[tuple[PreparedCap, ...], dict[str, bytes]]:
    if not requests:
        return (), {}
    if not materialize:
        return _materialize_remote_entry_requests_serial(
            requests,
            materialize=materialize,
            remote_cache=remote_cache,
            progress=progress,
        )
    for request in requests:
        _emit_remote_entry_pending(request, progress=progress)
    entries: list[PreparedCap] = []
    files: dict[str, bytes] = {}
    results: list[tuple[PreparedCap, dict[str, bytes]] | None] = [None] * len(
        requests
    )
    first_error: BaseException | None = None
    executor = ThreadPoolExecutor(max_workers=REMOTE_CAP_MATERIALIZE_WORKERS)
    try:
        futures = {
            executor.submit(
                _remote_entry_from_request,
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
    requests: tuple[_RemoteEntryRequest, ...],
    *,
    materialize: bool,
    remote_cache: _RemoteEntryCache | None,
    progress: ProgressSink | None,
) -> tuple[tuple[PreparedCap, ...], dict[str, bytes]]:
    entries: list[PreparedCap] = []
    files: dict[str, bytes] = {}
    for request in requests:
        entry, entry_files = _remote_entry_from_request(
            request,
            materialize=materialize,
            remote_cache=remote_cache,
            progress=progress,
        )
        entries.append(entry)
        files.update(entry_files)
    return tuple(sorted(entries, key=_entry_sort_key)), files


def _remote_entry_from_request(
    request: _RemoteEntryRequest,
    *,
    materialize: bool,
    remote_cache: _RemoteEntryCache | None,
    progress: ProgressSink | None,
) -> tuple[PreparedCap, dict[str, bytes]]:
    return _remote_entry_from_ref(
        visibility=request.visibility,
        kind=request.kind,
        ref=request.ref,
        name=request.name,
        relative_config_path=request.relative_config_path,
        source_fingerprint=request.source_fingerprint,
        source_mtime_ns=request.source_mtime_ns,
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
            ref = canonicalize_remote_ref(request.kind, ref)
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
    *,
    kind: EntryKind,
    name: str,
    form: Literal["wired", "ref"],
) -> Path:
    bucket = "cited" if form == "ref" else "wired"
    root = Path(bucket) / DIR_NAME_BY_KIND[kind] / name
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
