"""Canonical, content-addressed Agent State layer storage."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from typing import Literal, cast
from uuid import uuid4

from toolang.common.layout import AgentLayout

from ..common.immutable import freeze_mapping
from ..lang.ast import Program
from .source import SourceTree
from .state import (
    CapResolution,
    StateCap,
    StateModule,
    public_runnable_catalog,
)

LayerScope = Literal["root", "home"]
LAYER_SCHEMA = 1
AGENT_STATE_SCHEMA = 1
_LAYER_FILE = "layer.json"
_LAYERS_FILE = "layers.json"
_FILES_DIR = "files"
_REVS_DIR = "revs"


@dataclass(frozen=True, slots=True)
class RootLayer:
    """One immutable root State layer shared by all agents."""

    revision: str
    revision_dir: Path
    source: SourceTree
    resolutions: tuple[CapResolution, ...]
    config: Mapping[str, object]
    caps: tuple[StateCap, ...]

    def __post_init__(self) -> None:
        _require_revision(self.revision)
        if self.revision_dir.name != self.revision:
            raise ValueError("root State layer directory does not match its revision")
        object.__setattr__(self, "config", freeze_mapping(self.config))


@dataclass(frozen=True, slots=True)
class HomeLayer:
    """One immutable State layer for an agent home."""

    revision: str
    revision_dir: Path
    source: SourceTree
    resolutions: tuple[CapResolution, ...]
    config: Mapping[str, object]
    program: Program
    caps: tuple[StateCap, ...]
    modules: tuple[StateModule, ...]

    def __post_init__(self) -> None:
        _require_revision(self.revision)
        if self.revision_dir.name != self.revision:
            raise ValueError("home State layer directory does not match its revision")
        object.__setattr__(self, "config", freeze_mapping(self.config))


def state_root(layout: AgentLayout, scope: LayerScope) -> Path:
    """Return the persistent directory for one State layer scope."""

    return layout.root_state if scope == "root" else layout.home_state


def layer_current_path(layout: AgentLayout, scope: LayerScope) -> Path:
    return state_root(layout, scope) / "current"


def layer_lock_path(layout: AgentLayout, scope: LayerScope) -> Path:
    return state_root(layout, scope) / "prepare.lock"


def layer_revision_dir(
    layout: AgentLayout,
    scope: LayerScope,
    revision: str,
) -> Path:
    _require_revision(revision)
    return state_root(layout, scope) / _REVS_DIR / revision


def agent_current_path(layout: AgentLayout) -> Path:
    return layout.agent_state / "current"


def agent_lock_path(layout: AgentLayout) -> Path:
    return layout.agent_state / "prepare.lock"


def agent_revision_dir(layout: AgentLayout, revision: str) -> Path:
    _require_revision(revision)
    return layout.agent_state / _REVS_DIR / revision


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
) -> SourceTree:
    """Load the source tree recorded by one trusted State layer."""

    document, _ = _load_layer(layout, scope, revision)
    return _source_tree(document)


def load_root_layer(
    layout: AgentLayout,
    revision: str | None = None,
) -> RootLayer:
    """Load one trusted root State layer without integrity validation."""

    effective = revision or load_current_revision(layout, "root")
    document, revision_dir = _load_layer(layout, "root", effective)
    return RootLayer(
        revision=effective,
        revision_dir=revision_dir,
        source=_source_tree(document),
        resolutions=_resolutions(document),
        config=_config(document),
        caps=_caps(document, revision_dir=revision_dir),
    )


def load_home_layer(
    layout: AgentLayout,
    revision: str | None = None,
) -> HomeLayer:
    """Load one trusted home State layer without integrity validation."""

    effective = revision or load_current_revision(layout, "home")
    document, revision_dir = _load_layer(layout, "home", effective)
    modules = _modules(document, revision_dir=revision_dir)
    agent_module = next(module for module in modules if module.kind == "agent")
    return HomeLayer(
        revision=effective,
        revision_dir=revision_dir,
        source=_source_tree(document),
        resolutions=_resolutions(document),
        config=_config(document),
        program=agent_module.program,
        caps=_caps(document, revision_dir=revision_dir),
        modules=modules,
    )


def write_layer(
    *,
    layout: AgentLayout,
    scope: LayerScope,
    source: SourceTree,
    resolutions: tuple[CapResolution, ...],
    config: Mapping[str, object],
    caps: tuple[StateCap, ...],
    modules: tuple[StateModule, ...],
    files: Mapping[str, bytes],
) -> str:
    """Atomically store one complete immutable State layer."""

    normalized_files = _normalized_files(files)
    document = _layer_document(
        scope=scope,
        source=source,
        resolutions=resolutions,
        config=config,
        caps=caps,
        modules=modules,
        files=normalized_files,
    )
    encoded = canonical_json(document)
    revision = sha256(encoded).hexdigest()
    target = layer_revision_dir(layout, scope, revision)
    revs = target.parent
    revs.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        return revision
    staging = revs / f".{revision}.tmp-{uuid4().hex}"
    try:
        staging.mkdir()
        (staging / _LAYER_FILE).write_bytes(encoded)
        files_dir = staging / _FILES_DIR
        files_dir.mkdir()
        for relative, content in normalized_files.items():
            destination = files_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return revision


def publish_layer_current(
    layout: AgentLayout,
    scope: LayerScope,
    revision: str,
) -> None:
    """Atomically publish one already persisted State layer."""

    _write_revision(layer_current_path(layout, scope), revision)


def persist_agent_revision(
    layout: AgentLayout,
    *,
    root_revision: str,
    home_revision: str,
) -> str:
    """Persist and publish one exact root/home Agent State composition."""

    with _file_lock(agent_lock_path(layout)):
        document = agent_layers_document(
            root_revision=root_revision,
            home_revision=home_revision,
        )
        encoded = canonical_json(document)
        revision = sha256(encoded).hexdigest()
        target = agent_revision_dir(layout, revision)
        revs = target.parent
        revs.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            _write_revision(agent_current_path(layout), revision)
            return revision
        staging = revs / f".{revision}.tmp-{uuid4().hex}"
        try:
            staging.mkdir()
            (staging / _LAYERS_FILE).write_bytes(encoded)
            os.replace(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        _write_revision(agent_current_path(layout), revision)
        return revision


def load_agent_revisions(
    layout: AgentLayout,
    revision: str | None = None,
) -> tuple[str, str, str]:
    """Load one trusted Agent State composition without integrity validation."""

    effective = revision or load_current_agent_revision(layout)
    revision_dir = agent_revision_dir(layout, effective)
    document = _load_object(revision_dir / _LAYERS_FILE, label="layers.json")
    root_revision = _revision_field(document, "root_revision")
    home_revision = _revision_field(document, "home_revision")
    return effective, root_revision, home_revision


def validate_layer_revision(
    layout: AgentLayout,
    scope: LayerScope,
    revision: str,
) -> None:
    """Explicitly validate one layer document and its complete file manifest."""

    _validate_layer_dir(
        layer_revision_dir(layout, scope, revision),
        scope=scope,
        revision=revision,
    )


def validate_agent_revision(layout: AgentLayout, revision: str) -> None:
    """Explicitly validate one Agent State revision and both referenced layers."""

    document = _validate_agent_dir(
        agent_revision_dir(layout, revision),
        revision=revision,
    )
    validate_layer_revision(
        layout,
        "root",
        _revision_field(document, "root_revision"),
    )
    validate_layer_revision(
        layout,
        "home",
        _revision_field(document, "home_revision"),
    )


@contextmanager
def layer_lock(layout: AgentLayout, scope: LayerScope) -> Iterator[None]:
    """Serialize preparation writers for one State layer."""

    with _file_lock(layer_lock_path(layout, scope)):
        yield


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
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


def agent_layers_document(
    *,
    root_revision: str,
    home_revision: str,
) -> dict[str, object]:
    _require_revision(root_revision)
    _require_revision(home_revision)
    return {
        "home_revision": home_revision,
        "root_revision": root_revision,
        "schema": AGENT_STATE_SCHEMA,
    }


def _layer_document(
    *,
    scope: LayerScope,
    source: SourceTree,
    resolutions: tuple[CapResolution, ...],
    config: Mapping[str, object],
    caps: tuple[StateCap, ...],
    modules: tuple[StateModule, ...],
    files: Mapping[str, bytes],
) -> dict[str, object]:
    if scope == "root" and modules:
        raise ValueError("root State layer cannot contain program modules")
    return {
        "caps": [cap.to_data() for cap in sorted(caps, key=_cap_key)],
        "config": dict(config),
        "files": [
            {
                "path": f"files/{path}",
                "sha256": sha256(content).hexdigest(),
                "size": len(content),
            }
            for path, content in files.items()
        ],
        "modules": [
            module.to_data() for module in sorted(modules, key=lambda item: item.name)
        ],
        "resolutions": [
            item.to_data() for item in sorted(resolutions, key=_resolution_key)
        ],
        "schema": LAYER_SCHEMA,
        "scope": scope,
        "source": source.to_data(),
    }


def _load_layer(
    layout: AgentLayout,
    scope: LayerScope,
    revision: str,
) -> tuple[dict[str, object], Path]:
    revision_dir = layer_revision_dir(layout, scope, revision)
    return _load_object(revision_dir / _LAYER_FILE, label="layer.json"), revision_dir


def _validate_layer_dir(
    revision_dir: Path,
    *,
    scope: LayerScope,
    revision: str,
) -> dict[str, object]:
    _validate_top_level(revision_dir, required={_LAYER_FILE, _FILES_DIR})
    encoded = (revision_dir / _LAYER_FILE).read_bytes()
    document = _canonical_object(encoded, label="layer.json")
    if sha256(encoded).hexdigest() != revision:
        raise ValueError("State layer revision does not match layer.json")
    required = {
        "caps",
        "config",
        "files",
        "modules",
        "resolutions",
        "schema",
        "scope",
        "source",
    }
    if set(document) != required:
        raise ValueError("layer.json fields do not match the State layer schema")
    if document["schema"] != LAYER_SCHEMA:
        raise ValueError(f"unsupported State layer schema: {document['schema']!r}")
    if document["scope"] != scope:
        raise ValueError(
            f"State layer scope mismatch: expected {scope!r}, found {document['scope']!r}"
        )
    _source_tree(document)
    _config(document)
    manifest = _validate_file_manifest(revision_dir, document)
    resolutions = _resolutions(document)
    caps = _caps(document, revision_dir=revision_dir)
    modules = _modules(document, revision_dir=revision_dir, required=scope == "home")
    if scope == "root" and modules:
        raise ValueError("root State layer cannot contain program modules")
    if any(cap.scope != scope for cap in caps):
        raise ValueError(f"{scope} State layer contains a cap from another scope")
    referenced = {file.path for resolution in resolutions for file in resolution.files}
    referenced.update(resolution.definition for resolution in resolutions)
    referenced.update(resolution.materialized for resolution in resolutions)
    referenced.update(_document_file_references(document))
    missing = referenced - set(manifest)
    if missing:
        raise FileNotFoundError(
            f"State layer references files outside its manifest: {sorted(missing)!r}"
        )
    for module in modules:
        if manifest[module.materialized_path][1] != module.digest:
            raise ValueError(f"State module digest mismatch: {module.name}")
    for resolution in resolutions:
        for file in resolution.files:
            if manifest[file.path] != (file.size, file.sha256):
                raise ValueError(
                    f"resolved cap file does not match the manifest: {file.path}"
                )
    all_caps = (*caps, *(cap for module in modules for cap in module.here_caps))
    for resolution in resolutions:
        matches = tuple(
            cap
            for cap in all_caps
            if (
                cap.kind == resolution.kind
                and cap.name == resolution.name
                and cap.source.form == resolution.form
                and cap.source.declared_ref == resolution.declared_ref
                and cap.ref == resolution.resolved_ref
                and _stored_cap_path(cap, revision_dir=revision_dir)
                == resolution.materialized
            )
        )
        if len(matches) != 1:
            raise ValueError(
                f"resolved cap does not match exactly one State capability: "
                f"{resolution.kind}/{resolution.name}"
            )
    return document


def _stored_cap_path(cap: StateCap, *, revision_dir: Path) -> str:
    path = Path(cap.path)
    return (
        path.relative_to(revision_dir).as_posix()
        if path.is_absolute()
        else path.as_posix()
    )


def _document_file_references(document: Mapping[str, object]) -> set[str]:
    references: set[str] = set()
    raw_caps = document.get("caps", [])
    raw_modules = document.get("modules", [])
    if not isinstance(raw_caps, list) or not isinstance(raw_modules, list):
        raise TypeError("State layer caps and modules must be lists")
    for raw in raw_caps:
        if isinstance(raw, dict):
            raw_cap = cast(dict[object, object], raw)
            references.add(str(raw_cap.get("path", "")))
    for raw in raw_modules:
        if not isinstance(raw, dict):
            continue
        raw_module = cast(dict[object, object], raw)
        references.add(str(raw_module.get("materialized_path", "")))
        raw_here_caps = raw_module.get("here_caps", [])
        if not isinstance(raw_here_caps, list):
            raise TypeError("State module here_caps must be a list")
        for raw_cap in raw_here_caps:
            if isinstance(raw_cap, dict):
                cap = cast(dict[object, object], raw_cap)
                references.add(str(cap.get("path", "")))
    for path in references:
        _layer_file_path(path)
    return references


def _validate_agent_dir(
    revision_dir: Path,
    *,
    revision: str,
) -> dict[str, object]:
    _validate_top_level(revision_dir, required={_LAYERS_FILE})
    encoded = (revision_dir / _LAYERS_FILE).read_bytes()
    document = _canonical_object(encoded, label="layers.json")
    if sha256(encoded).hexdigest() != revision:
        raise ValueError("Agent State revision does not match layers.json")
    if set(document) != {"home_revision", "root_revision", "schema"}:
        raise ValueError("layers.json fields do not match the Agent State schema")
    if document["schema"] != AGENT_STATE_SCHEMA:
        raise ValueError(f"unsupported Agent State schema: {document['schema']!r}")
    _revision_field(document, "root_revision")
    _revision_field(document, "home_revision")
    return document


def _validate_file_manifest(
    revision_dir: Path,
    document: Mapping[str, object],
) -> dict[str, tuple[int, str]]:
    raw_files = document.get("files")
    if not isinstance(raw_files, list):
        raise TypeError("State layer files must be a list")
    declared: dict[str, tuple[int, str]] = {}
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise TypeError("State layer file must be an object")
        item = cast(dict[object, object], raw)
        path = str(item.get("path", ""))
        _layer_file_path(path)
        size = item.get("size")
        digest = str(item.get("sha256", ""))
        if not isinstance(size, int) or isinstance(size, bool):
            raise TypeError("State layer file size must be an integer")
        _require_revision(digest)
        if path in declared:
            raise ValueError(f"duplicate State layer file: {path}")
        declared[path] = (size, digest)
    if list(declared) != sorted(declared):
        raise ValueError("State layer files must be sorted by path")
    files_dir = revision_dir / _FILES_DIR
    actual: dict[str, Path] = {}
    for path in sorted(files_dir.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"State layer files cannot be symbolic links: {path}")
        if path.is_file():
            actual[f"files/{path.relative_to(files_dir).as_posix()}"] = path
    if set(actual) != set(declared):
        missing = sorted(set(declared) - set(actual))
        extra = sorted(set(actual) - set(declared))
        raise ValueError(
            f"State layer file manifest mismatch: missing={missing!r}, extra={extra!r}"
        )
    for relative, path in actual.items():
        expected_size, expected_hash = declared[relative]
        content = path.read_bytes()
        if len(content) != expected_size:
            raise ValueError(f"State layer file size mismatch: {relative}")
        if sha256(content).hexdigest() != expected_hash:
            raise ValueError(f"State layer file hash mismatch: {relative}")
    return declared


def _caps(
    document: Mapping[str, object],
    *,
    revision_dir: Path,
) -> tuple[StateCap, ...]:
    raw_caps = document.get("caps")
    if not isinstance(raw_caps, list):
        raise TypeError("State layer caps must be a list")
    caps = tuple(_state_cap(raw, revision_dir=revision_dir) for raw in raw_caps)
    if tuple(sorted(caps, key=_cap_key)) != caps:
        raise ValueError("State layer caps must be sorted")
    if len({_cap_key(cap) for cap in caps}) != len(caps):
        raise ValueError("State layer caps must be unique")
    return caps


def _modules(
    document: Mapping[str, object],
    *,
    revision_dir: Path,
    required: bool = True,
) -> tuple[StateModule, ...]:
    raw_modules = document.get("modules")
    if not isinstance(raw_modules, list):
        raise TypeError("State layer modules must be a list")
    if required and not raw_modules:
        raise ValueError("home State layer requires program modules")
    modules: list[StateModule] = []
    names: set[str] = set()
    for raw in raw_modules:
        if not isinstance(raw, dict):
            raise TypeError("State module must be an object")
        module = StateModule.from_data(
            {str(key): value for key, value in cast(dict[object, object], raw).items()}
        )
        folded_name = module.name.casefold()
        if folded_name in names:
            raise ValueError(f"duplicate State module: {module.name}")
        names.add(folded_name)
        modules.append(
            replace(
                module,
                here_caps=tuple(
                    _materialize_cap(cap, revision_dir=revision_dir)
                    for cap in module.here_caps
                ),
            )
        )
    result = tuple(modules)
    if tuple(sorted(result, key=lambda item: item.name)) != result:
        raise ValueError("State modules must be sorted by name")
    if required and sum(module.kind == "agent" for module in result) != 1:
        raise ValueError("home State layer requires exactly one agent module")
    if result:
        public_runnable_catalog(result)
    return result


def _state_cap(raw: object, *, revision_dir: Path) -> StateCap:
    if not isinstance(raw, dict):
        raise TypeError("State cap must be an object")
    cap = StateCap.from_data(
        {str(key): value for key, value in cast(dict[object, object], raw).items()}
    )
    return _materialize_cap(cap, revision_dir=revision_dir)


def _materialize_cap(cap: StateCap, *, revision_dir: Path) -> StateCap:
    relative = _layer_file_path(cap.path)
    path = revision_dir / relative
    return replace(cap, path=str(path))


def _resolutions(document: Mapping[str, object]) -> tuple[CapResolution, ...]:
    raw = document.get("resolutions")
    if not isinstance(raw, list):
        raise TypeError("State layer resolutions must be a list")
    result = tuple(
        CapResolution.from_data(cast(dict[str, object], item))
        for item in raw
        if isinstance(item, dict)
    )
    if len(result) != len(raw):
        raise TypeError("State layer resolution must be an object")
    if tuple(sorted(result, key=_resolution_key)) != result:
        raise ValueError("State layer resolutions must be sorted")
    if len({_resolution_key(item) for item in result}) != len(result):
        raise ValueError("State layer resolutions must be unique")
    return result


def _source_tree(document: Mapping[str, object]) -> SourceTree:
    raw = document.get("source")
    if not isinstance(raw, dict):
        raise TypeError("State layer source must be an object")
    return SourceTree.from_data(
        {str(key): value for key, value in cast(dict[object, object], raw).items()}
    )


def _config(document: Mapping[str, object]) -> dict[str, object]:
    raw = document.get("config")
    if not isinstance(raw, dict):
        raise TypeError("State layer config must be an object")
    return {str(key): value for key, value in raw.items()}


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
        or path.as_posix() != value
    ):
        raise ValueError(
            f"State layer file path must be portable and relative: {value!r}"
        )
    return path


def _layer_file_path(value: str) -> Path:
    path = _relative_file_path(value)
    if path.parts[:1] != (_FILES_DIR,) or len(path.parts) < 2:
        raise ValueError(f"State layer path must be inside files/: {value!r}")
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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    temporary.write_text(f"{revision}\n", encoding="utf-8")
    os.replace(temporary, path)


def _revision_field(document: Mapping[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    _require_revision(value)
    return value


def _require_revision(value: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("revision must be a lowercase SHA-256 hex digest")


def _cap_key(cap: StateCap) -> tuple[str, str, str]:
    return cap.kind, cap.name, cap.ref


def _resolution_key(item: CapResolution) -> tuple[str, str, str, str, str]:
    return (
        item.kind,
        item.name,
        item.form,
        item.definition,
        item.materialized,
    )
