"""Canonical, content-addressed Agent State layer storage."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from typing import Literal, cast
from uuid import uuid4

from toolang.common.layout import AgentLayout, validate_agent_name
from toolang.common.files import atomic_write_text, ensure_directory, ensure_file
from toolang.common.progress import ProgressSink, emit_progress

from ..common.immutable import freeze_mapping, mutable_data
from ..lang.ast import Program, Span, program_from_data
from .types import StateFile, EntryKind, CapForm
from .config import (
    CAP_ALLOW_FIELDS,
    WORKSPACE_ORDER_KEY,
    resolve_cap_allows,
    validate_workspace_name,
)
from toolang.catalog.chores import Chore, inline_chores, merge_chores
from .source import (
    SourceManifest,
    SourceManifestEntry,
    SourceIssue,
)
from .state import (
    AGENT_STATE_FORMAT,
    agent_layers_document,
    CapResolution,
    StateCap,
    public_runnable_index,
    validate_program_term,
    CapSource,
    MaterializedFile,
    runnable_module_name,
)

LayerScope = Literal["root", "home"]
_LAYER_FILE = "layer.json"
_LAYERS_FILE = "layers.json"
_SOURCE_DIR = "source"
_MATERIALIZED_DIR = "materialized"
_REVS_DIR = "revs"


@dataclass(frozen=True, slots=True)
class RootLayer:
    """One immutable root State layer shared by all agents."""

    revision: str
    revision_dir: Path
    schema: int
    source: SourceManifest
    resolutions: tuple[CapResolution, ...]
    config: Mapping[str, object]
    caps: tuple[StateCap, ...]
    created_at: str = ""
    records: tuple[StateFile, ...] = ()

    def __post_init__(self) -> None:
        _require_revision(self.revision)
        if self.revision_dir.name != self.revision:
            raise ValueError("root State layer directory does not match its revision")
        if self.schema == AGENT_STATE_FORMAT and (
            not isinstance(self.source, SourceManifest)
        ):
            raise ValueError("current root State layer requires a source manifest")
        object.__setattr__(self, "config", freeze_mapping(self.config))


@dataclass(frozen=True, slots=True)
class HomeLayer:
    """One immutable State layer for an agent home."""

    revision: str
    revision_dir: Path
    schema: int
    source: SourceManifest
    resolutions: tuple[CapResolution, ...]
    config: Mapping[str, object]
    caps: tuple[StateCap, ...]
    modules: Mapping[str, Program]
    module_sources: Mapping[str, str]
    module_digests: Mapping[str, str]
    module_caps: Mapping[str, tuple[StateCap, ...]]
    created_at: str = ""
    module_resolutions: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    records: tuple[StateFile, ...] = ()
    chores: tuple[Chore, ...] = ()

    def __post_init__(self) -> None:
        _require_revision(self.revision)
        if self.revision_dir.name != self.revision:
            raise ValueError("home State layer directory does not match its revision")
        if self.schema == AGENT_STATE_FORMAT and (
            not isinstance(self.source, SourceManifest)
        ):
            raise ValueError("current home State layer requires a source manifest")
        object.__setattr__(self, "config", freeze_mapping(self.config))
        object.__setattr__(self, "modules", freeze_mapping(self.modules))
        object.__setattr__(self, "module_sources", freeze_mapping(self.module_sources))
        object.__setattr__(self, "module_digests", freeze_mapping(self.module_digests))
        object.__setattr__(self, "module_caps", freeze_mapping(self.module_caps))
        object.__setattr__(
            self, "module_resolutions", freeze_mapping(self.module_resolutions)
        )


def state_root(layout: AgentLayout, scope: LayerScope) -> Path:
    """Return this format's persistent directory for one State layer scope."""

    anchor = layout.place_state if scope == "root" else layout.home_state
    return anchor / f"v{AGENT_STATE_FORMAT}"


def _agent_state_root(layout: AgentLayout) -> Path:
    return layout.agent_state / f"v{AGENT_STATE_FORMAT}"


def layer_current_path(layout: AgentLayout, scope: LayerScope) -> Path:
    return state_root(layout, scope) / "current"


def layer_build_lock_path(layout: AgentLayout, scope: LayerScope) -> Path:
    return state_root(layout, scope) / "build.lock"


def layer_revision_dir(
    layout: AgentLayout,
    scope: LayerScope,
    revision: str,
) -> Path:
    _require_revision(revision)
    return state_root(layout, scope) / _REVS_DIR / revision


def agent_current_path(layout: AgentLayout) -> Path:
    return _agent_state_root(layout) / "current"


def _agent_publish_lock_path(layout: AgentLayout) -> Path:
    return _agent_state_root(layout) / "publish.lock"


def agent_revision_dir(layout: AgentLayout, revision: str) -> Path:
    _require_revision(revision)
    return _agent_state_root(layout) / _REVS_DIR / revision


def load_current_revision(layout: AgentLayout, scope: LayerScope) -> str:
    """Load one layer's current revision pointer."""

    return _read_revision(layer_current_path(layout, scope))


def load_current_agent_revision(layout: AgentLayout) -> str:
    """Load the current Agent State revision pointer."""

    return _read_revision(agent_current_path(layout))


def load_layer_source(
    layout: AgentLayout,
    scope: LayerScope,
    revision: str,
) -> SourceManifest:
    """Load the source identity recorded by one trusted State layer."""

    document, _ = _load_layer(layout, scope, revision)
    return _source_record(document)


def load_root_layer(layout: AgentLayout, revision: str | None = None) -> RootLayer:
    effective = load_current_revision(layout, "root") if revision is None else revision
    document, directory = _load_layer(layout, "root", effective)
    caps = _decode_caps(document, directory, layout.name)
    _decode_chores(document, modules={}, agent_name=layout.name)
    return RootLayer(
        revision=effective,
        revision_dir=directory,
        schema=AGENT_STATE_FORMAT,
        created_at=_timestamp(document),
        source=_source_record(document),
        resolutions=_cap_resolutions(document),
        config=_config(document),
        caps=caps,
        records=_source_receipts(document),
    )


def load_home_layer(layout: AgentLayout, revision: str | None = None) -> HomeLayer:
    effective = load_current_revision(layout, "home") if revision is None else revision
    document, directory = _load_layer(layout, "home", effective)
    modules, sources, digests = _decode_modules(document)
    caps = _decode_caps(
        document, directory, layout.name, modules=modules, module_sources=sources
    )
    shared = tuple(cap for cap in caps if cap.scope != "here")
    local = {
        name: tuple(
            cap
            for cap in caps
            if cap.scope == "here" and _source_key(cap, "home", layout.name) == source
        )
        for name, source in sources.items()
    }
    return HomeLayer(
        revision=effective,
        revision_dir=directory,
        schema=AGENT_STATE_FORMAT,
        created_at=_timestamp(document),
        source=_source_record(document),
        resolutions=_cap_resolutions(document),
        config=_config(document),
        caps=shared,
        records=_source_receipts(document),
        modules=modules,
        module_sources=sources,
        module_digests=digests,
        module_caps=local,
        chores=_decode_chores(document, modules=modules, agent_name=layout.name),
        module_resolutions={
            str(item["ref"]): cast(dict[str, str], item["resolution"])
            for item in _items(document, "modules")
            if "resolution" in item
        },
    )


def write_layer(
    *,
    layout: AgentLayout,
    scope: LayerScope,
    source: SourceManifest,
    config: Mapping[str, object],
    caps: tuple[StateCap, ...],
    modules: Mapping[str, Program],
    module_sources: Mapping[str, str],
    module_caps: Mapping[str, tuple[StateCap, ...]],
    files: Mapping[str, bytes],
    records: tuple[StateFile, ...] = (),
    chores: tuple[Chore, ...] = (),
    module_resolutions: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    normalized = _normalized_files(files)
    document = _layer_document(
        scope=scope,
        source=source,
        config=config,
        caps=caps,
        modules=modules,
        module_sources=module_sources,
        module_caps=module_caps,
        files=normalized,
        records=records,
        chores=chores,
        agent_name=layout.name,
        module_resolutions=module_resolutions or {},
    )
    try:
        current = load_current_revision(layout, scope)
        previous, directory = _load_layer(layout, scope, current)
        if canonical_json(_without_time(previous)) == canonical_json(document):
            _validate_layer_dir(
                directory, scope=scope, revision=current, agent_name=layout.name
            )
            return current
    except (OSError, KeyError, TypeError, ValueError):
        pass
    document["created_at"] = _creation_time()
    encoded = canonical_json(document)
    revision = sha256(encoded).hexdigest()
    target = layer_revision_dir(layout, scope, revision)
    ensure_directory(target.parent, inherit_owner=True)
    staging = target.with_name(f".{revision}.tmp-{uuid4().hex}")
    try:
        staging.mkdir()
        (staging / _LAYER_FILE).write_bytes(encoded)
        (staging / _SOURCE_DIR).mkdir()
        (staging / _MATERIALIZED_DIR).mkdir()
        for key, content in normalized.items():
            destination = staging / _layer_file_path(key)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as output:
                output.write(content)
        _validate_layer_dir(
            staging, scope=scope, revision=revision, agent_name=layout.name
        )
        _install_revision(
            staging,
            target,
            validate=lambda path: _is_valid_layer_dir(
                path, scope=scope, revision=revision, agent_name=layout.name
            ),
        )
    finally:
        _remove_path(staging)
    return revision


def publish_layer_current(
    layout: AgentLayout, scope: LayerScope, revision: str
) -> None:
    _write_revision(layer_current_path(layout, scope), revision)


def _persist_agent_revision(
    layout: AgentLayout,
    *,
    root_revision: str,
    home_revision: str,
    allow_overrides: Mapping[str, tuple[str, ...]] | None = None,
    workspace_additions: Mapping[str, str] | None = None,
    publish: bool = True,
) -> str:
    document = agent_layers_document(
        root_revision=root_revision,
        home_revision=home_revision,
        name=layout.name,
        allow_overrides=allow_overrides,
        workspace_additions=workspace_additions,
    )
    try:
        current = load_current_agent_revision(layout)
        previous = _load_object(
            agent_revision_dir(layout, current) / _LAYERS_FILE, label=_LAYERS_FILE
        )
        if canonical_json(_without_time(previous)) == canonical_json(document):
            _validate_agent_dir(agent_revision_dir(layout, current), revision=current)
            return current
    except (OSError, KeyError, TypeError, ValueError):
        pass
    document["created_at"] = _creation_time()
    encoded = canonical_json(document)
    revision = sha256(encoded).hexdigest()
    target = agent_revision_dir(layout, revision)
    ensure_directory(target.parent, inherit_owner=True)
    staging = target.with_name(f".{revision}.tmp-{uuid4().hex}")
    try:
        staging.mkdir()
        (staging / _LAYERS_FILE).write_bytes(encoded)
        _install_revision(
            staging,
            target,
            validate=lambda path: _is_valid_agent_dir(path, revision=revision),
        )
    finally:
        _remove_path(staging)
    if publish:
        publish_agent_current(layout, revision)
    return revision


def publish_agent_current(layout: AgentLayout, revision: str) -> None:
    """Publish only after the captured composition has passed runtime validation."""
    _write_revision(agent_current_path(layout), revision)


def load_agent_revisions(
    layout: AgentLayout, revision: str | None = None
) -> tuple[str, str, str, str, dict[str, tuple[str, ...]], dict[str, str], str]:
    effective = load_current_agent_revision(layout) if revision is None else revision
    document = _load_object(
        agent_revision_dir(layout, effective) / _LAYERS_FILE, label=_LAYERS_FILE
    )
    _check_agent_document(document)
    overrides = cast(dict[str, list[str]], document["allow_overrides"])
    return (
        effective,
        _revision_field(document, "root_revision"),
        _revision_field(document, "home_revision"),
        str(document["name"]),
        {key: tuple(value) for key, value in overrides.items()},
        dict(cast(list[tuple[str, str]], document.get("workspace_additions", []))),
        _timestamp(document),
    )


def validate_layer_revision(
    layout: AgentLayout, scope: LayerScope, revision: str
) -> None:
    _validate_layer_dir(
        layer_revision_dir(layout, scope, revision),
        scope=scope,
        revision=revision,
        agent_name=layout.name,
    )


def validate_agent_revision(layout: AgentLayout, revision: str) -> None:
    document = _validate_agent_dir(
        agent_revision_dir(layout, revision), revision=revision
    )
    validate_layer_revision(layout, "root", _revision_field(document, "root_revision"))
    validate_layer_revision(layout, "home", _revision_field(document, "home_revision"))


@contextmanager
def layer_build_lock(
    layout: AgentLayout, scope: LayerScope, *, progress: ProgressSink | None = None
) -> Iterator[None]:
    """Serialize preparation writers for one State layer."""

    with _file_lock(
        layer_build_lock_path(layout, scope), label=f"{scope} state", progress=progress
    ):
        yield


@contextmanager
def _agent_publish_lock(
    layout: AgentLayout, *, progress: ProgressSink | None = None
) -> Iterator[None]:
    """Serialize one agent's complete check and publication across processes."""

    with _file_lock(
        _agent_publish_lock_path(layout),
        label=f"agent {layout.name} state",
        progress=progress,
    ):
        yield


@contextmanager
def _file_lock(
    path: Path, *, label: str, progress: ProgressSink | None
) -> Iterator[None]:
    ensure_file(path, inherit_owner=True)
    with path.open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            emit_progress(
                progress,
                id=f"state-lock:{path}",
                kind="prepare",
                stage="materialize",
                label=f"Waiting for {label}...",
                status="running",
            )
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except BaseException:
                emit_progress(
                    progress,
                    id=f"state-lock:{path}",
                    kind="prepare",
                    stage="materialize",
                    label=f"Failed to acquire {label}",
                    status="failed",
                )
                raise
            emit_progress(
                progress,
                id=f"state-lock:{path}",
                kind="prepare",
                stage="materialize",
                label=f"{label.capitalize()} available",
                status="ok",
            )
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def canonical_json(value: object) -> bytes:
    """Encode the canonical JSON used as State revision identity."""

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _creation_time() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _timestamp(document: Mapping[str, object]) -> str:
    value = document.get("created_at")
    if not isinstance(value, str):
        raise ValueError("State revision requires created_at")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as error:
        raise ValueError(
            "created_at must be a UTC timestamp with microseconds"
        ) from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != value:
        raise ValueError("created_at must have fixed-width microseconds")
    return value


def _without_time(document: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in document.items() if key != "created_at"}


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _items(document: Mapping[str, object], key: str) -> tuple[dict[str, object], ...]:
    value = document.get(key)
    if not isinstance(value, list):
        raise TypeError(f"{key} must be a list")
    return tuple(_object(item, key) for item in value)


def _file_maps(document: Mapping[str, object]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for directory in (_SOURCE_DIR, _MATERIALIZED_DIR):
        records = _object(document.get(directory), directory)
        for key, value in records.items():
            _relative_file_path(key)
            record = _object(value, "file record")
            required = {"size", "sha256"}
            if set(record) - {"error"} != required or (
                directory == _MATERIALIZED_DIR and "error" in record
            ):
                raise ValueError("file record fields do not match the schema")
            size, digest = record["size"], record["sha256"]
            if size is None or digest is None:
                if size is not None or digest is not None or "error" not in record:
                    raise ValueError(
                        "unreadable source requires null size/digest and error"
                    )
            else:
                if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                    raise ValueError("file size must be a non-negative integer")
                if not isinstance(digest, str):
                    raise TypeError("file digest must be a string")
                _require_revision(digest)
            if "error" in record:
                error = _object(record["error"], "source error")
                if set(error) - {"line"} != {"code", "message"} or not all(
                    isinstance(error[field], str) for field in ("code", "message")
                ):
                    raise ValueError("invalid source error")
                line = error.get("line")
                if line is not None and (
                    not isinstance(line, int) or isinstance(line, bool) or line < 1
                ):
                    raise ValueError("invalid source error line")
            result[f"{directory}/{key}"] = record
    return result


def _source_record(document: Mapping[str, object]) -> SourceManifest:
    files: list[SourceManifestEntry] = []
    issues: list[SourceIssue] = []
    for key, raw in _object(document.get("source"), "source").items():
        record = _object(raw, "source record")
        digest = record["sha256"]
        if isinstance(digest, str):
            files.append(
                SourceManifestEntry(key, cast(int, record["size"]), digest, digest)
            )
        else:
            error = _object(record["error"], "source error")
            issues.append(SourceIssue(key, str(error["code"]), str(error["message"])))
    return SourceManifest(
        tuple(sorted(files, key=lambda item: item.path)),
        issues=tuple(sorted(issues, key=lambda item: item.path)),
    )


def _source_receipts(document: Mapping[str, object]) -> tuple[StateFile, ...]:
    result: list[StateFile] = []
    scope = cast(LayerScope, document["scope"])
    for key, raw in sorted(_object(document.get("source"), "source").items()):
        record = _object(raw, "source record")
        error = _object(record["error"], "source error") if "error" in record else {}
        result.append(
            StateFile(
                scope,
                key,
                cast(str | None, record["sha256"]),
                not error,
                cast(str | None, error.get("code")),
                cast(str | None, error.get("message")),
                cast(int | None, error.get("line")),
            )
        )
    return tuple(result)


def _source_key(cap: StateCap, scope: str, agent_name: str) -> str:
    path = Path(cap.source.path)
    if (
        cap.kind == "skill"
        and cap.source.form == "authored"
        and path.name != "SKILL.md"
    ):
        path /= "SKILL.md"
    return (
        path.relative_to(Path("agents") / agent_name).as_posix()
        if scope == "home"
        else path.as_posix()
    )


def _cap_resolutions(document: Mapping[str, object]) -> tuple[CapResolution, ...]:
    result: list[CapResolution] = []
    materialized = _object(document["materialized"], "materialized")
    for item in _items(document, "caps"):
        if "resolution" not in item:
            continue
        resolution = _object(item["resolution"], "resolution")
        kind, name = str(item["ref"]).split("/", 1)
        entry = str(resolution["materialized"])
        root = Path(entry).parent
        selected = [
            key
            for key in sorted(materialized)
            if key == entry or (kind == "skill" and Path(key).is_relative_to(root))
        ]
        files = tuple(
            MaterializedFile(
                f"materialized/{key}",
                cast(int, _object(materialized[key], "file")["size"]),
                str(_object(materialized[key], "file")["sha256"]),
            )
            for key in selected
        )
        digest = sha256()
        for file in files:
            digest.update(f"{file.path}\0{file.sha256}\n".encode())
        result.append(
            CapResolution(
                kind=cast(EntryKind, kind),
                name=name,
                form=cast(CapForm, item["form"]),
                declared_ref=str(resolution["declared_ref"]),
                resolved_ref=str(resolution["resolved_ref"]),
                definition=f"source/{item['source']}",
                materialized=f"materialized/{entry}",
                content_hash=digest.hexdigest(),
                files=files,
            )
        )
    return tuple(result)


def _decode_modules(
    document: Mapping[str, object],
) -> tuple[dict[str, Program], dict[str, str], dict[str, str]]:
    modules: dict[str, Program] = {}
    sources: dict[str, str] = {}
    digests: dict[str, str] = {}
    inputs = _object(document["source"], "source")
    for item in _items(document, "modules"):
        if set(item) - {"resolution"} != {"ref", "source", "value"}:
            raise ValueError("module fields do not match the schema")
        name, source = str(item["ref"]), item["source"]
        if name in modules:
            raise ValueError("duplicate State module")
        if source is None:
            if name != "agent":
                raise ValueError("only the default agent Program can omit source")
            if "agent.too" in inputs and "error" not in _object(
                inputs["agent.too"], "agent source"
            ):
                raise ValueError("an accepted agent source must own its Program")
            path, digest = "agent.too", sha256(b"").hexdigest()
        else:
            path = str(source)
            record = _object(inputs[path], "module source")
            if "error" in record:
                raise ValueError("module references an excluded source")
            expected = "agent" if path == "agent.too" else runnable_module_name(path)
            if name not in {expected, f"runnables::{expected}"}:
                raise ValueError("module name does not match source")
            name = expected
            if name in modules:
                raise ValueError("duplicate State module")
            digest = str(record["sha256"])
        program = program_from_data(item["value"])
        if source is None and program != Program(span=Span(1)):
            raise ValueError("a source-free agent Program must be empty")
        if "resolution" in item:
            _check_resolution(
                _object(item["resolution"], "module resolution"),
                _object(document["materialized"], "materialized"),
            )
            if source is None:
                raise ValueError("an acquired module requires its captured source")
            entry = str(_object(item["resolution"], "resolution")["materialized"])
            if _object(document["materialized"], "materialized")[entry] != inputs[path]:
                raise ValueError(
                    "acquired module bytes do not match its captured source"
                )
        validate_program_term(
            name=name, source=path, digest=digest, program=program, here_caps=()
        )
        modules[name], sources[name], digests[name] = program, path, digest
    if document["scope"] == "home" and "agent" not in modules:
        raise ValueError("home State layer requires the agent Program")
    if document["scope"] == "root" and modules:
        raise ValueError("root State layer cannot contain modules")
    if modules:
        public_runnable_index(modules, sources)
    return (
        dict(sorted(modules.items())),
        dict(sorted(sources.items())),
        dict(sorted(digests.items())),
    )


def _decode_caps(
    document: Mapping[str, object],
    directory: Path,
    agent_name: str,
    *,
    modules: Mapping[str, Program] | None = None,
    module_sources: Mapping[str, str] | None = None,
) -> tuple[StateCap, ...]:
    result: list[StateCap] = []
    scope = str(document["scope"])
    inputs = _object(document["source"], "source")
    materialized = _object(document["materialized"], "materialized")
    if modules is None or module_sources is None:
        modules, module_sources, _ = _decode_modules(document)
    seen: set[tuple[str, str]] = set()
    for item in _items(document, "caps"):
        if set(item) - {"resolution"} != {"ref", "form", "source", "value"}:
            raise ValueError("cap fields do not match the schema")
        ref = str(item["ref"])
        kind, name = ref.split("/", 1)
        if (
            not name.strip()
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or kind not in {"skill", "psyche", "service", "prompt"}
        ):
            raise ValueError("invalid cap kind or name")
        form, source = str(item["form"]), str(item["source"])
        record = _object(inputs[source], "cap source")
        if "error" in record:
            raise ValueError("cap references an excluded source")
        here = form in {"inline", "referenced"}
        if form not in {"authored", "configured", "inline", "referenced"}:
            raise ValueError("invalid cap form")
        if form == "configured" and source != "config.toml":
            raise ValueError("configured Cap must reference its config source")
        expected_source = (
            f"skills/{name}/SKILL.md" if kind == "skill" else f"{kind}s/{name}.md"
        )
        if form == "authored" and source != expected_source:
            raise ValueError("authored Cap source must match its kind and name")
        if here and (scope != "home" or source not in module_sources.values()):
            raise ValueError("module-local cap has no owning module")
        identity = (source if here else "", ref)
        if identity in seen:
            raise ValueError("conflicting State capabilities in one scope")
        seen.add(identity)
        value = _object(item["value"], "cap value")
        if set(value) != {"meta", "body"} or not isinstance(value["body"], str):
            raise ValueError("cap value requires meta and body")
        meta = _object(value["meta"], "cap metadata")
        line = None
        if here:
            program = modules[
                next(key for key, path in module_sources.items() if path == source)
            ]
            if form == "inline":
                lines = [
                    cap.span.line
                    for cap in program.caps
                    if cap.kind == kind and cap.name == name
                ]
            else:
                declared = _object(item.get("resolution"), "resolution").get(
                    "declared_ref"
                )
                lines = [
                    use.span.line
                    for use in program.withs
                    if use.cap_kind == kind and use.reference == declared
                ]
            if not lines or (form == "inline" and len(lines) != 1):
                raise ValueError("cap does not match its Program declaration")
            line = lines[0]
        remote = form in {"configured", "referenced"}
        if remote != ("resolution" in item):
            raise ValueError("remote caps require one complete resolution")
        resource_digest = sha256()
        resource_digest.update(str(record["sha256"]).encode())
        if remote:
            resolution = _object(item["resolution"], "resolution")
            _check_resolution(resolution, materialized)
            canonical = str(resolution["resolved_ref"])
            entry = str(resolution["materialized"])
            path = f"materialized/{entry}"
            records = materialized
        else:
            canonical = f"{('inline' if here else scope)}://{kind}s/{name}"
            entry, path, records = source, f"source/{source}", inputs
        if kind == "skill" and not here or remote:
            keys = (
                [entry]
                if kind != "skill"
                else [
                    key
                    for key in sorted(records)
                    if Path(key).is_relative_to(Path(entry).parent)
                ]
            )
            for key in keys:
                resource_digest.update(
                    f"{key}\0{_object(records[key], 'file')['sha256']}\n".encode()
                )
        fingerprint = (
            resource_digest.hexdigest()
            if kind == "skill" or remote
            else str(record["sha256"])
        )
        definition = (
            Path(source).parent.as_posix()
            if kind == "skill" and form == "authored"
            else source
        )
        provenance = (
            f"agents/{agent_name}/{definition}" if scope == "home" else definition
        )
        result.append(
            StateCap(
                kind=cast(EntryKind, kind),
                name=name,
                shape="dir" if kind == "skill" and form != "inline" else "file",
                ref=canonical,
                path=str(directory / path),
                source=CapSource(
                    origin="remote" if remote else "local",
                    form=cast(CapForm, form),
                    path=provenance,
                    updated_at="1970-01-01T00:00:00+00:00",
                    fingerprint=fingerprint,
                    declared_ref=str(
                        _object(item["resolution"], "resolution")["declared_ref"]
                    )
                    if remote
                    else None,
                    line=line,
                ),
                meta=meta,
                body=str(value["body"]),
                materialized=entry if remote else None,
                resources=tuple(
                    MaterializedFile(
                        f"materialized/{key}",
                        cast(int, _object(materialized[key], "file")["size"]),
                        str(_object(materialized[key], "file")["sha256"]),
                    )
                    for key in sorted(materialized)
                    if remote and Path(key).parts[0] == Path(entry).parts[0]
                ),
            )
        )
    return tuple(result)


def _check_resolution(
    resolution: dict[str, object], materialized: dict[str, object]
) -> None:
    if set(resolution) != {"declared_ref", "resolved_ref", "materialized"} or not all(
        isinstance(value, str) and value for value in resolution.values()
    ):
        raise ValueError("resolution requires declared_ref, resolved_ref, materialized")
    key, entry = str(resolution["resolved_ref"]), str(resolution["materialized"])
    path = _relative_file_path(entry)
    if (
        len(path.parts) < 2
        or path.parts[0] != sha256(key.encode()).hexdigest()
        or entry not in materialized
    ):
        raise ValueError("resolution does not match its materialized acquisition")


def _decode_chores(
    document: Mapping[str, object],
    *,
    agent_name: str,
    modules: Mapping[str, Program] | None = None,
) -> tuple[Chore, ...]:
    result: list[Chore] = []
    seen: set[str] = set()
    inputs = _object(document["source"], "source")
    for item in _items(document, "chores"):
        if set(item) != {"ref", "source", "value"}:
            raise ValueError("chore fields do not match the schema")
        ref, source = str(item["ref"]), str(item["source"])
        if not ref.startswith("chore/") or document["scope"] != "home":
            raise ValueError("invalid chore ref or scope")
        if "error" in _object(inputs[source], "chore source"):
            raise ValueError("chore references excluded source")
        value = _object(item["value"], "chore value")
        if set(value) != {"title", "body", "schedule"} or not isinstance(
            value["body"], str
        ):
            raise ValueError("invalid chore value")
        for key in ("title", "schedule"):
            text = value[key]
            if text is not None and (
                not isinstance(text, str) or not text.strip() or text != text.strip()
            ):
                raise ValueError(f"chore {key} must be normalized text or null")
        line = None
        if (
            source != "agent.too"
            and source != f"chores/{ref.removeprefix('chore/')}.md"
        ):
            raise ValueError("chore source does not match its name")
        if source == "agent.too":
            if modules is None:
                modules = _decode_modules(document)[0]
            program = modules["agent"]
            matches = [
                chore.span.line
                for chore in program.chores
                if chore.name == ref.removeprefix("chore/")
            ]
            if len(matches) != 1:
                raise ValueError("chore does not match Program")
            line = matches[0]
        chore = Chore(
            ref.removeprefix("chore/"),
            cast(str | None, value["title"]),
            str(value["body"]),
            cast(str | None, value["schedule"]),
            Path(source),
            line,
            agent_name=agent_name,
        )
        identity = chore.id
        if identity in seen:
            raise ValueError("duplicate chore")
        seen.add(identity)
        result.append(chore)
    return tuple(result)


def _config(document: Mapping[str, object]) -> dict[str, object]:
    config = _object(document.get("config"), "State config")
    if set(config) - {"workspaces", "allow", WORKSPACE_ORDER_KEY}:
        raise ValueError("config contains fields not owned by State")
    workspaces = _object(config.get("workspaces", {}), "workspaces")
    for name, path in workspaces.items():
        validate_workspace_name(name)
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError("stored workspace paths must be absolute")
    order = config.get(WORKSPACE_ORDER_KEY)
    if order is not None and (
        not isinstance(order, list)
        or not all(isinstance(name, str) for name in order)
        or len(order) != len(workspaces)
        or set(order) != set(workspaces)
    ):
        raise ValueError("workspace order must list each configured workspace once")
    allow = _object(config.get("allow", {}), "allow")
    if set(allow) - set(CAP_ALLOW_FIELDS):
        raise ValueError("config contains unsupported State allow fields")
    resolve_cap_allows((config,))
    return config


def _layer_document(
    *,
    scope: LayerScope,
    source: SourceManifest,
    config: Mapping[str, object],
    caps: tuple[StateCap, ...],
    modules: Mapping[str, Program],
    module_sources: Mapping[str, str],
    module_caps: Mapping[str, tuple[StateCap, ...]],
    files: Mapping[str, bytes],
    records: tuple[StateFile, ...],
    chores: tuple[Chore, ...],
    agent_name: str,
    module_resolutions: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    from toolang.lang.ast import to_data
    import frontmatter

    manifests: dict[str, dict[str, object]] = {"source": {}, "materialized": {}}
    for path, content in files.items():
        relative = _layer_file_path(path)
        manifests[relative.parts[0]][Path(*relative.parts[1:]).as_posix()] = {
            "size": len(content),
            "sha256": sha256(content).hexdigest(),
        }
    observed = {item.path: item for item in source.files}
    selected = records or tuple(
        StateFile(scope, item.path, item.raw_digest or item.digest)
        for item in source.files
    )
    for record in selected:
        if not record.accepted:
            original = observed.get(record.key)
            error: dict[str, object] = {"code": record.code, "message": record.message}
            if record.line is not None:
                error["line"] = record.line
            manifests["source"][record.key] = {
                "size": original.size if original else None,
                "sha256": record.digest,
                "error": error,
            }

    def cap_item(cap: StateCap) -> dict[str, object]:
        key = _source_key(cap, scope, agent_name)
        if cap.body is not None:
            body = cap.body
        else:
            post = frontmatter.loads(files[cap.path].decode("utf-8"))
            body = post.content.strip()
        result: dict[str, object] = {
            "ref": cap.effective_ref,
            "form": cap.source.form,
            "source": key,
            "value": {"meta": mutable_data(cap.meta), "body": body},
        }
        if cap.source.origin == "remote":
            result["resolution"] = {
                "declared_ref": cap.source.declared_ref,
                "resolved_ref": cap.ref,
                "materialized": Path(cap.path).relative_to("materialized").as_posix(),
            }
        return result

    all_caps = (*caps, *(cap for values in module_caps.values() for cap in values))
    combined = (
        merge_chores(
            chores,
            inline_chores(
                modules["agent"].chores, path=Path("agent.too"), agent_name=agent_name
            ),
        )
        if "agent" in modules
        else {}
    )
    return {
        "schema": AGENT_STATE_FORMAT,
        "scope": scope,
        "config": {
            key: value
            for key, value in config.items()
            if key not in {"skills", "psyches", "services", "prompts"}
        },
        "source": manifests["source"],
        "materialized": manifests["materialized"],
        "modules": [
            {
                "ref": name,
                "source": module_sources[name]
                if module_sources[name] in manifests["source"]
                and "error"
                not in _object(
                    manifests["source"][module_sources[name]], "module source"
                )
                else None,
                "value": to_data(program),
                **(
                    {"resolution": dict(module_resolutions[name])}
                    if name in module_resolutions
                    else {}
                ),
            }
            for name, program in sorted(modules.items())
        ],
        "caps": [
            cap_item(cap)
            for cap in sorted(
                all_caps,
                key=lambda cap: (cap.effective_ref, cap.source.path, cap.source.form),
            )
        ],
        "chores": [
            {
                "ref": chore.ref,
                "source": chore.path.as_posix(),
                "value": {
                    "title": chore.title,
                    "body": chore.body,
                    "schedule": chore.schedule,
                },
            }
            for chore in sorted(combined.values(), key=lambda chore: chore.ref)
        ],
    }


def _check_layer_document(
    document: Mapping[str, object], *, scope: LayerScope
) -> dict[str, dict[str, object]]:
    if (
        type(document.get("schema")) is not int
        or document["schema"] != AGENT_STATE_FORMAT
    ):
        raise ValueError(f"unsupported State layer schema: {document.get('schema')!r}")
    if set(document) != {
        "schema",
        "scope",
        "created_at",
        "config",
        "source",
        "materialized",
        "modules",
        "caps",
        "chores",
    }:
        raise ValueError("layer.json fields do not match the State layer schema")
    if document["scope"] != scope:
        raise ValueError("unsupported State layer scope")
    _timestamp(document)
    _config(document)
    records = _file_maps(document)
    materialized = _object(document["materialized"], "materialized")
    acquisitions = set()
    for collection in ("modules", "caps"):
        for item in _items(document, collection):
            if "resolution" in item:
                resolution = _object(item["resolution"], "resolution")
                _check_resolution(resolution, materialized)
                acquisitions.add(
                    sha256(str(resolution["resolved_ref"]).encode()).hexdigest()
                )
    if any(
        len(Path(key).parts) < 2 or Path(key).parts[0] not in acquisitions
        for key in materialized
    ):
        raise ValueError("materialized file has no owning acquisition")
    return records


def _load_layer(
    layout: AgentLayout, scope: LayerScope, revision: str
) -> tuple[dict[str, object], Path]:
    directory = layer_revision_dir(layout, scope, revision)
    document = _load_object(directory / _LAYER_FILE, label=_LAYER_FILE)
    _check_layer_document(document, scope=scope)
    return document, directory


def _validate_layer_dir(
    directory: Path, *, scope: LayerScope, revision: str, agent_name: str
) -> dict[str, object]:
    _validate_top_level(
        directory, required={_LAYER_FILE, _SOURCE_DIR, _MATERIALIZED_DIR}
    )
    encoded = (directory / _LAYER_FILE).read_bytes()
    document = _canonical_object(encoded, label=_LAYER_FILE)
    if sha256(encoded).hexdigest() != revision:
        raise ValueError("State layer revision does not match layer.json")
    records = _check_layer_document(document, scope=scope)
    expected = {key: record for key, record in records.items() if "error" not in record}
    actual: dict[str, Path] = {}
    for name in (_SOURCE_DIR, _MATERIALIZED_DIR):
        if not (directory / name).is_dir():
            raise ValueError("State file store must be a directory")
        for path in (directory / name).rglob("*"):
            if path.is_symlink():
                raise ValueError("State files cannot be symbolic links")
            if path.is_file():
                actual[path.relative_to(directory).as_posix()] = path
    if set(actual) != set(expected):
        raise ValueError("State layer file manifest mismatch")
    for key, path in actual.items():
        content = path.read_bytes()
        if (
            len(content) != expected[key]["size"]
            or sha256(content).hexdigest() != expected[key]["sha256"]
        ):
            raise ValueError(f"State file size or hash mismatch: {key}")
    modules, sources, _ = _decode_modules(document)
    _decode_caps(
        document, directory, "validation", modules=modules, module_sources=sources
    )
    _decode_chores(document, modules=modules, agent_name=agent_name)
    return document


def _check_agent_document(document: Mapping[str, object]) -> None:
    if (
        type(document.get("schema")) is not int
        or document["schema"] != AGENT_STATE_FORMAT
    ):
        raise ValueError("unsupported Agent State schema")
    if set(document) - {"workspace_additions"} != {
        "schema",
        "created_at",
        "root_revision",
        "home_revision",
        "name",
        "allow_overrides",
    }:
        raise ValueError("layers.json fields do not match the Agent State schema")
    _timestamp(document)
    _revision_field(document, "root_revision")
    _revision_field(document, "home_revision")
    validate_agent_name(str(document["name"]))
    overrides = _object(document["allow_overrides"], "allow_overrides")
    resolve_cap_allows((), overrides=cast(dict[str, tuple[str, ...]], overrides))
    additions = document.get("workspace_additions", [])
    if not isinstance(additions, list):
        raise ValueError("workspace additions must be an ordered list")
    seen = set()
    for pair in additions:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("workspace additions require name/path pairs")
        name, path = pair
        if (
            not isinstance(name, str)
            or not isinstance(path, str)
            or not Path(path).is_absolute()
        ):
            raise ValueError("invalid workspace addition")
        validate_workspace_name(name)
        if name in seen or name == "lab":
            raise ValueError("duplicate or reserved workspace addition")
        seen.add(name)


def _validate_agent_dir(directory: Path, *, revision: str) -> dict[str, object]:
    _validate_top_level(directory, required={_LAYERS_FILE})
    encoded = (directory / _LAYERS_FILE).read_bytes()
    document = _canonical_object(encoded, label=_LAYERS_FILE)
    if sha256(encoded).hexdigest() != revision:
        raise ValueError("Agent State revision does not match layers.json")
    _check_agent_document(document)
    return document


def _is_valid_layer_dir(
    path: Path, *, scope: LayerScope, revision: str, agent_name: str
) -> bool:
    try:
        _validate_layer_dir(path, scope=scope, revision=revision, agent_name=agent_name)
    except (OSError, KeyError, TypeError, ValueError):
        return False
    return True


def _is_valid_agent_dir(path: Path, *, revision: str) -> bool:
    try:
        _validate_agent_dir(path, revision=revision)
    except (OSError, KeyError, TypeError, ValueError):
        return False
    return True


def _install_revision(
    staging: Path,
    target: Path,
    *,
    validate: Callable[[Path], bool],
) -> None:
    """Install staging, replacing an invalid same-revision entry if necessary."""

    _inherit_revision_owner(staging)
    while True:
        if not _path_exists(target):
            try:
                os.replace(staging, target)
            except OSError:
                if _path_exists(target):
                    continue
                raise
            return
        if validate(target):
            return
        displaced = target.with_name(f".{target.name}.invalid-{uuid4().hex}")
        try:
            os.replace(target, displaced)
        except FileNotFoundError:
            continue
        try:
            if _path_exists(target):
                continue
            try:
                os.replace(staging, target)
            except OSError:
                if _path_exists(target):
                    continue
                os.replace(displaced, target)
                raise
            return
        finally:
            _remove_path(displaced)


def _inherit_revision_owner(staging: Path) -> None:
    """Keep only this newly built cache tree accessible to its mounted owner."""
    if os.geteuid() != 0:
        return
    owner = staging.parent.stat()
    for directory, _children, files in os.walk(staging):
        os.chown(directory, owner.st_uid, owner.st_gid, follow_symlinks=False)
        for name in files:
            os.chown(
                Path(directory) / name,
                owner.st_uid,
                owner.st_gid,
                follow_symlinks=False,
            )


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _canonical_object(encoded: bytes, *, label: str) -> dict[str, object]:
    value = json.loads(encoded.decode("utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{label} must contain an object")
    document = {str(key): item for key, item in value.items()}
    if canonical_json(document) != encoded:
        raise ValueError(f"{label} is not canonical JSON")
    return document


def _load_object(path: Path, *, label: str) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{label} must contain an object")
    return {str(key): item for key, item in value.items()}


def _normalized_files(files: Mapping[str, bytes]) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for value, content in files.items():
        relative = _relative_file_path(value.replace("\\", "/"))
        key = relative.as_posix()
        if key in result:
            raise ValueError(f"duplicate State layer file: {key}")
        result[key] = bytes(content)
    return dict(sorted(result.items()))


def _relative_file_path(value: str) -> Path:
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or not path.parts
        or path.as_posix() != value
    ):
        raise ValueError(
            f"State layer file path must be portable and relative: {value!r}"
        )
    return path


def _layer_file_path(value: str) -> Path:
    path = _relative_file_path(value)
    if path.parts[0] not in {_SOURCE_DIR, _MATERIALIZED_DIR} or len(path.parts) < 2:
        raise ValueError(
            f"State layer path must be inside source/ or materialized/: {value!r}"
        )
    return path


def _validate_top_level(path: Path, *, required: set[str]) -> None:
    if not path.is_dir() or path.is_symlink():
        raise FileNotFoundError(f"State revision directory not found: {path}")
    actual = {item.name for item in path.iterdir()}
    if actual != required:
        raise ValueError(
            f"State revision layout mismatch: expected={sorted(required)!r}, "
            f"found={sorted(actual)!r}"
        )
    for name in required:
        if (path / name).is_symlink():
            raise ValueError(
                f"State revision entries cannot be symbolic links: {path / name}"
            )


def _read_revision(path: Path) -> str:
    revision = path.read_text(encoding="utf-8").strip()
    _require_revision(revision)
    return revision


def _write_revision(path: Path, revision: str) -> None:
    _require_revision(revision)
    try:
        if _read_revision(path) == revision:
            return
    except (FileNotFoundError, TypeError, ValueError):
        pass
    atomic_write_text(path, f"{revision}\n", inherit_owner=True)


def _revision_field(document: Mapping[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    _require_revision(value)
    return value


def _require_revision(value: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("revision must be a lowercase SHA-256 hex digest")
