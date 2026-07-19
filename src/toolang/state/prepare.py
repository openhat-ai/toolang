"""Build self-contained root and home prepared generations."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import logging
from pathlib import Path
import tomllib
from typing import cast

from ..common.progress import ProgressSink
from ..lang.ast import Program, to_data
from .agent import AgentState, compose_agent_state
from .caps import generation_remote_cache, materialize_visibility
from .durable import (
    DurableFile,
    DurableState,
    scan_durable_state,
    scan_root_durable_state,
)
from .generation import (
    HomePrepared,
    PreparedScope,
    RootPrepared,
    load_current_version,
    load_generation_source,
    load_home_prepared,
    load_root_prepared,
    prepare_lock,
    prepared_generation_dir,
    publish_current,
    write_generation,
)
from .prepared import PreparedEntry, PreparedVisibility
from .source import SourceTree, scan_home_source, scan_root_source

_PREPARED_SCHEMA = 1
_MAX_SOURCE_SNAPSHOT_ATTEMPTS = 3
logger = logging.getLogger("toolang.prepare")


def prepare_agent_state(
    toolang_root: Path,
    agent_name: str,
    *,
    toolang_version: str,
    force: bool = False,
    progress: ProgressSink | None = None,
) -> AgentState:
    """Prepare and compose the immutable runtime state for one agent."""

    root, home = prepare_generations(
        toolang_root,
        agent_name,
        toolang_version=toolang_version,
        force=force,
        progress=progress,
    )
    return compose_agent_state(root, home)


def refresh_agent_state(
    toolang_root: Path,
    agent_name: str,
    *,
    toolang_version: str,
    progress: ProgressSink | None = None,
) -> AgentState:
    """Explicitly refresh remote resolutions and prepare one agent state."""

    return prepare_agent_state(
        toolang_root,
        agent_name,
        toolang_version=toolang_version,
        force=True,
        progress=progress,
    )


def prepare_generations(
    toolang_root: Path,
    agent_name: str,
    *,
    toolang_version: str,
    force: bool = False,
    progress: ProgressSink | None = None,
) -> tuple[RootPrepared, HomePrepared]:
    """Prepare and load the shared root and one agent home."""

    _require_root(toolang_root)
    _require_agent_home(toolang_root, agent_name)
    root = prepare_root(
        toolang_root,
        toolang_version=toolang_version,
        force=force,
        progress=progress,
    )
    home = prepare_home(
        toolang_root,
        agent_name,
        toolang_version=toolang_version,
        force=force,
        progress=progress,
    )
    return root, home


def prepare_root(
    toolang_root: Path,
    *,
    toolang_version: str,
    force: bool = False,
    progress: ProgressSink | None = None,
) -> RootPrepared:
    """Build or reuse the root generation shared by every agent."""

    _require_root(toolang_root)
    source = scan_root_source(toolang_root)
    current = _matching_root(
        toolang_root,
        source=source,
        force=force,
    )
    if current is not None:
        return current
    with prepare_lock(toolang_root):
        source = scan_root_source(toolang_root)
        current = _matching_root(
            toolang_root,
            source=source,
            force=force,
        )
        if current is not None:
            return current
        _build_stable_generation(
            toolang_root,
            None,
            scope="root",
            visibility="shared",
            toolang_version=toolang_version,
            reuse_remote=not force,
            progress=progress,
        )
        return load_root_prepared(toolang_root)


def prepare_home(
    toolang_root: Path,
    agent_name: str,
    *,
    toolang_version: str,
    force: bool = False,
    progress: ProgressSink | None = None,
) -> HomePrepared:
    """Build or reuse one agent-home generation."""

    _require_agent_home(toolang_root, agent_name)
    source = scan_home_source(toolang_root, agent_name)
    current = _matching_home(
        toolang_root,
        agent_name,
        source=source,
        force=force,
    )
    if current is not None:
        return current
    with prepare_lock(toolang_root, agent_name):
        source = scan_home_source(toolang_root, agent_name)
        current = _matching_home(
            toolang_root,
            agent_name,
            source=source,
            force=force,
        )
        if current is not None:
            return current
        _build_stable_generation(
            toolang_root,
            agent_name,
            scope="home",
            visibility="private",
            toolang_version=toolang_version,
            reuse_remote=not force,
            progress=progress,
        )
        return load_home_prepared(toolang_root, agent_name)


def _matching_root(
    toolang_root: Path,
    *,
    source: SourceTree,
    force: bool,
) -> RootPrepared | None:
    if force:
        return None
    try:
        version = load_current_version(toolang_root)
        generation_dir = prepared_generation_dir(toolang_root, version)
        if load_generation_source(generation_dir) != source:
            return None
        return load_root_prepared(toolang_root, version)
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None


def _matching_home(
    toolang_root: Path,
    agent_name: str,
    *,
    source: SourceTree,
    force: bool,
) -> HomePrepared | None:
    if force:
        return None
    try:
        version = load_current_version(toolang_root, agent_name)
        generation_dir = prepared_generation_dir(toolang_root, version, agent_name)
        if load_generation_source(generation_dir) != source:
            return None
        return load_home_prepared(toolang_root, agent_name, version)
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return None


def _build_stable_generation(
    toolang_root: Path,
    agent_name: str | None,
    *,
    scope: PreparedScope,
    visibility: PreparedVisibility,
    toolang_version: str,
    reuse_remote: bool,
    progress: ProgressSink | None,
) -> bytes:
    for _ in range(_MAX_SOURCE_SNAPSHOT_ATTEMPTS):
        source = _scan_scope_source(toolang_root, agent_name, scope=scope)
        durable = (
            scan_root_durable_state(toolang_root)
            if scope == "root"
            else scan_durable_state(toolang_root, _required_agent_name(agent_name))
        )
        previous_entries = (
            _previous_generation_entries(
                toolang_root,
                agent_name=agent_name,
                scope=scope,
            )
            if reuse_remote
            else ()
        )
        remote_cache = generation_remote_cache(
            durable,
            visibility=visibility,
            entries=previous_entries,
        )
        entries, generated_files = materialize_visibility(
            durable,
            visibility=visibility,
            remote_cache=remote_cache or None,
            progress=progress,
        )
        files = _snapshot_files(
            durable,
            generated_files,
            visibility=visibility,
        )
        entries = tuple(
            _snapshot_entry(
                entry,
                agent_name=durable.agent_name,
                files=files,
                visibility=visibility,
            )
            for entry in entries
        )
        resolved = _resolved_document(entries, files)
        prepared = _prepared_document(
            entries,
            durable=durable,
            files=files,
            scope=scope,
            toolang_version=toolang_version,
        )
        if source != _scan_scope_source(toolang_root, agent_name, scope=scope):
            continue
        version = write_generation(
            toolang_root=toolang_root,
            agent_name=agent_name,
            scope=scope,
            source=source,
            resolved=resolved,
            prepared=prepared,
            files=files,
        )
        publish_current(
            toolang_root,
            version,
            agent_name,
        )
        return version
    raise RuntimeError(f"{scope} source changed repeatedly while preparing")


def _previous_generation_entries(
    toolang_root: Path,
    *,
    agent_name: str | None,
    scope: PreparedScope,
) -> tuple[PreparedEntry, ...]:
    try:
        if scope == "root":
            return load_root_prepared(toolang_root).caps
        return load_home_prepared(
            toolang_root, _required_agent_name(agent_name)
        ).caps
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return ()


def _scan_scope_source(
    toolang_root: Path,
    agent_name: str | None,
    *,
    scope: PreparedScope,
) -> SourceTree:
    if scope == "root":
        return scan_root_source(toolang_root)
    return scan_home_source(toolang_root, _required_agent_name(agent_name))


def _required_agent_name(agent_name: str | None) -> str:
    if agent_name is None:
        raise ValueError("home preparation requires an agent name")
    return agent_name


def _require_root(toolang_root: Path) -> None:
    if not toolang_root.is_dir():
        raise FileNotFoundError(f"Toolang root not found: {toolang_root}")


def _require_agent_home(toolang_root: Path, agent_name: str) -> None:
    home = toolang_root / "agents" / agent_name
    if not home.is_dir():
        raise FileNotFoundError(f"agent home not found: {home}")


def _snapshot_files(
    durable: DurableState,
    generated_files: dict[str, bytes],
    *,
    visibility: PreparedVisibility,
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for item in durable.files:
        if not _file_belongs_to_visibility(item, visibility=visibility):
            continue
        target = _durable_snapshot_path(
            item,
            agent_name=durable.agent_name,
            visibility=visibility,
        )
        files[str(target)] = item.content
    for path, content in generated_files.items():
        target = _generated_snapshot_path(Path(path))
        files[str(target)] = content
    return files


def _file_belongs_to_visibility(
    item: DurableFile,
    *,
    visibility: PreparedVisibility,
) -> bool:
    return item.origin == ("root" if visibility == "shared" else "agent")


def _durable_snapshot_path(
    item: DurableFile,
    *,
    agent_name: str,
    visibility: PreparedVisibility,
) -> Path:
    relative = _scope_relative_path(
        Path(item.relative_path),
        agent_name=agent_name,
        visibility=visibility,
    )
    if item.category == "cap":
        return Path("authored") / relative
    if item.category == "program":
        return Path("agent.too")
    if item.category == "config":
        return Path("config.toml")
    return Path("authored") / relative


def _generated_snapshot_path(
    path: Path,
) -> Path:
    if len(path.parts) < 3:
        raise ValueError(f"unexpected materialized cap path: {path}")
    if path.parts[0] not in {"inline", "referenced", "wired"}:
        raise ValueError(f"unexpected materialized cap bucket: {path.parts[0]}")
    return path


def _scope_relative_path(
    path: Path,
    *,
    agent_name: str,
    visibility: PreparedVisibility,
) -> Path:
    if visibility == "shared":
        return path
    prefix = Path("agents") / agent_name
    try:
        return path.relative_to(prefix)
    except ValueError as exc:
        raise ValueError(f"home source is outside the agent directory: {path}") from exc


def _snapshot_entry(
    entry: PreparedEntry,
    *,
    agent_name: str,
    files: dict[str, bytes],
    visibility: PreparedVisibility,
) -> PreparedEntry:
    path = _entry_snapshot_path(
        entry,
        agent_name=agent_name,
        visibility=visibility,
    )
    source = replace(entry.source, path=entry.source.path)
    if source.origin == "remote":
        source = replace(
            source,
            fingerprint=_remote_snapshot_fingerprint(
                source.fingerprint,
                path=path,
                shape=entry.shape,
                files=files,
            ),
        )
    return replace(
        entry,
        path=f"files/{path}",
        source=source,
    )


def _entry_snapshot_path(
    entry: PreparedEntry,
    *,
    agent_name: str,
    visibility: PreparedVisibility,
) -> Path:
    if entry.source.form == "file":
        relative = _scope_relative_path(
            Path(entry.path),
            agent_name=agent_name,
            visibility=visibility,
        )
        return Path("authored") / relative
    return _generated_snapshot_path(Path(entry.path))


def _remote_snapshot_fingerprint(
    authored_fingerprint: str,
    *,
    path: Path,
    shape: str,
    files: dict[str, bytes],
) -> str:
    selected = (
        [(str(path), files[str(path)])]
        if shape == "file" and str(path) in files
        else [
            (candidate, content)
            for candidate, content in files.items()
            if Path(candidate).is_relative_to(path.parent)
        ]
    )
    digest = sha256()
    digest.update(authored_fingerprint.encode("ascii"))
    digest.update(b"\0")
    for candidate, content in sorted(selected):
        digest.update(candidate.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256(content).digest())
        digest.update(b"\n")
    return digest.hexdigest()


def _resolved_document(
    entries: tuple[PreparedEntry, ...],
    files: dict[str, bytes],
) -> dict[str, object]:
    resolved: list[dict[str, object]] = []
    for entry in entries:
        if entry.source.origin != "remote":
            continue
        materialized = Path(entry.path)
        selected = _entry_materialized_files(entry, files)
        file_data = [
            {
                "path": f"files/{path}",
                "size": len(content),
                "sha256": sha256(content).hexdigest(),
            }
            for path, content in selected
        ]
        digest = sha256()
        for item in file_data:
            digest.update(str(item["path"]).encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(item["sha256"]).encode("ascii"))
            digest.update(b"\n")
        resolved.append(
            {
                "kind": entry.kind,
                "name": entry.name,
                "form": entry.source.form,
                "authored_ref": entry.source.authored_ref,
                "resolved_ref": entry.ref,
                "line": entry.source.line,
                "definition": f"files/{_resolved_definition_path(entry)}",
                "materialized": str(materialized),
                "content_hash": digest.hexdigest(),
                "files": file_data,
            }
        )
    resolved.sort(
        key=lambda item: (
            str(item["kind"]),
            str(item["name"]),
            str(item["form"]),
            str(item["definition"]),
        )
    )
    return {"schema": 1, "entries": resolved}


def _resolved_definition_path(entry: PreparedEntry) -> Path:
    if entry.source.form == "wired":
        return Path("config.toml")
    return Path("agent.too")


def _entry_materialized_files(
    entry: PreparedEntry,
    files: dict[str, bytes],
) -> list[tuple[str, bytes]]:
    path = Path(entry.path)
    relative = Path(*path.parts[1:])
    if entry.shape == "file":
        content = files.get(str(relative))
        return [] if content is None else [(str(relative), content)]
    root = relative.parent
    return sorted(
        (candidate, content)
        for candidate, content in files.items()
        if Path(candidate).is_relative_to(root)
    )


def _prepared_document(
    entries: tuple[PreparedEntry, ...],
    *,
    durable: DurableState,
    files: dict[str, bytes],
    scope: PreparedScope,
    toolang_version: str,
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": _PREPARED_SCHEMA,
        "scope": scope,
        "toolang_version": toolang_version,
        "config": _snapshot_config(files),
        "caps": [entry.to_data() for entry in entries],
    }
    if scope == "home":
        program: Program = durable.load_program().parse()
        document["program"] = cast(dict[str, object], to_data(program))
    return document


def _snapshot_config(files: dict[str, bytes]) -> dict[str, object]:
    content = files.get("config.toml")
    if content is None:
        return {}
    return cast(dict[str, object], tomllib.loads(content.decode("utf-8")))
