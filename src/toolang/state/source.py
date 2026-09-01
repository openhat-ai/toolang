"""Portable source manifests and captured authored file contents."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
from typing import Literal, cast

from toolang.catalog.types import CAP_DIRECTORY_NAMES

from ..lang.ast import Program, Span
from .config import canonical_state_config

SourceNodeKind = Literal["file", "directory"]
SOURCE_SCHEMA = 3
LEGACY_SOURCE_SCHEMA = 2
_AGENT_HEADER_RE = re.compile(r"^agent\s+[A-Za-z_][\w-]*\s*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class LegacySourceNode:
    """One node from a schema-2 metadata source tree."""

    name: str
    kind: SourceNodeKind
    mtime_ns: int
    size: int
    digest: str | None = None
    children: tuple[LegacySourceNode, ...] = ()

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> LegacySourceNode:
        """Load one schema-2 source node for historical revisions."""

        kind = str(data["type"])
        if kind not in {"file", "directory"}:
            raise ValueError(f"invalid source node type: {kind!r}")
        raw_children = cast(list[dict[str, object]], data.get("children", []))
        children = tuple(cls.from_data(child) for child in raw_children)
        if kind == "file" and children:
            raise ValueError("source file node cannot contain children")
        if tuple(sorted(children, key=lambda child: child.name)) != children:
            raise ValueError("source node children must be sorted by name")
        return cls(
            name=str(data["name"]),
            kind=cast(SourceNodeKind, kind),
            mtime_ns=_integer_field(data, "mtime_ns"),
            size=_integer_field(data, "size"),
            digest=(
                str(data["digest"]) if isinstance(data.get("digest"), str) else None
            ),
            children=children,
        )


@dataclass(frozen=True, slots=True)
class LegacySourceTree:
    """A schema-2 metadata tree retained only for historical layer loading."""

    root: LegacySourceNode
    schema: int = LEGACY_SOURCE_SCHEMA

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> LegacySourceTree:
        """Load a schema-2 tree without treating it as a current manifest."""

        schema = _integer_field(data, "schema")
        if schema != LEGACY_SOURCE_SCHEMA:
            raise ValueError(f"unsupported source schema: {schema}")
        raw_root = data.get("root")
        if not isinstance(raw_root, Mapping):
            raise TypeError("legacy source root must be an object")
        return cls(
            schema=schema,
            root=LegacySourceNode.from_data(cast(Mapping[str, object], raw_root)),
        )


@dataclass(frozen=True, slots=True)
class SourceObservationEntry:
    """Process-local stat identity for one selected logical source file."""

    path: str
    source: Path
    device: int
    inode: int
    mtime_ns: int
    size: int
    link_device: int | None = None
    link_inode: int | None = None
    link_mtime_ns: int | None = None
    link_size: int | None = None
    link_target: str | None = None


@dataclass(frozen=True, slots=True)
class SourceObservation:
    """Process-local selected paths, listings, and inexpensive stat facts."""

    files: tuple[SourceObservationEntry, ...]

    def __post_init__(self) -> None:
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("source observation paths must be sorted and unique")


@dataclass(frozen=True, slots=True)
class SourceManifestEntry:
    """Portable semantic identity for one selected source file."""

    path: str
    size: int
    digest: str

    def __post_init__(self) -> None:
        _portable_relative_path(self.path)
        if self.size < 0:
            raise ValueError("source manifest size must be non-negative")
        if _SHA256_RE.fullmatch(self.digest) is None:
            raise ValueError("source manifest digest must be a SHA-256 hex value")

    def to_data(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.digest, "size": self.size}

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> SourceManifestEntry:
        if set(data) != {"path", "sha256", "size"}:
            raise ValueError("source manifest file fields do not match schema")
        path = data.get("path")
        digest = data.get("sha256")
        size = data.get("size")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise TypeError("source manifest path and digest must be strings")
        if not isinstance(size, int) or isinstance(size, bool):
            raise TypeError("source manifest size must be an integer")
        return cls(path=path, size=size, digest=digest)


@dataclass(frozen=True, slots=True)
class SourceManifest:
    """Portable content identity of every selected file in one State scope."""

    files: tuple[SourceManifestEntry, ...]
    schema: int = SOURCE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SOURCE_SCHEMA:
            raise ValueError(f"unsupported source manifest schema: {self.schema}")
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("source manifest paths must be sorted and unique")

    def to_data(self) -> dict[str, object]:
        return {
            "files": [item.to_data() for item in self.files],
            "schema": self.schema,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, object]) -> SourceManifest:
        if set(data) != {"files", "schema"}:
            raise ValueError("source manifest fields do not match schema")
        schema = _integer_field(data, "schema")
        if schema != SOURCE_SCHEMA:
            raise ValueError(f"unsupported source manifest schema: {schema}")
        raw_files = data.get("files")
        if not isinstance(raw_files, list):
            raise TypeError("source manifest files must be an array")
        files = tuple(
            SourceManifestEntry.from_data(cast(Mapping[str, object], item))
            for item in raw_files
            if isinstance(item, Mapping)
        )
        if len(files) != len(raw_files):
            raise TypeError("source manifest file must be an object")
        return cls(files=files, schema=schema)


SourceRecord = SourceManifest | LegacySourceTree


class SourceChangedError(RuntimeError):
    """The selected source changed while its manifest was being captured."""


def scan_source(
    base: Path,
    paths: tuple[str, ...],
    *,
    project_configs: bool = False,
) -> SourceManifest:
    """Read and hash one complete portable source manifest."""

    observation = observe_source(base, paths)
    return build_source_manifest(
        observation,
        project_configs=project_configs,
    )


def scan_root_source(toolang_root: Path) -> SourceManifest:
    """Capture root config and authored cap paths."""

    return root_source_manifest(observe_root_source(toolang_root))


def scan_home_source(toolang_root: Path, agent_name: str) -> SourceManifest:
    """Capture one agent program, config, and authored cap paths."""

    return home_source_manifest(observe_home_source(toolang_root, agent_name))


def observe_source(base: Path, paths: Sequence[str]) -> SourceObservation:
    """Observe selected logical files without reading their contents."""

    selected: dict[str, Path] = {}
    for value in sorted(set(paths)):
        relative = _portable_relative_path(value)
        path = base / relative
        if not path.exists() and not path.is_symlink():
            continue
        for file in _selected_files(path):
            logical = file.relative_to(base).as_posix()
            selected[logical] = file
    return SourceObservation(
        files=tuple(
            _observe_file(path, relative_path=relative)
            for relative, path in sorted(selected.items())
        )
    )


def build_source_manifest(
    observation: SourceObservation,
    *,
    project_configs: bool,
    previous_observation: SourceObservation | None = None,
    previous_manifest: SourceManifest | None = None,
    invalidated: Collection[str] = (),
) -> SourceManifest:
    """Hash changed files and reuse unchanged facts from one prior observation."""

    previous_observed = (
        {item.path: item for item in previous_observation.files}
        if previous_observation is not None
        else {}
    )
    previous_files = (
        {item.path: item for item in previous_manifest.files}
        if previous_manifest is not None
        else {}
    )
    invalidated_paths = frozenset(invalidated)
    entries: list[SourceManifestEntry] = []
    for item in observation.files:
        if (
            item.path not in invalidated_paths
            and previous_observed.get(item.path) == item
            and previous_manifest is not None
        ):
            cached = previous_files.get(item.path)
            if cached is not None:
                entries.append(cached)
                continue
            if project_configs and item.path == "config.toml":
                continue
        content = item.source.read_bytes()
        if project_configs and item.path == "config.toml":
            content = canonical_state_config(content)
            if not content:
                if _observe_file(item.source, relative_path=item.path) != item:
                    raise SourceChangedError(
                        f"source changed while reading: {item.source}"
                    )
                continue
        if _observe_file(item.source, relative_path=item.path) != item:
            raise SourceChangedError(f"source changed while reading: {item.source}")
        entries.append(
            SourceManifestEntry(
                path=item.path,
                size=len(content),
                digest=sha256(content).hexdigest(),
            )
        )
    return SourceManifest(files=tuple(entries))


def observe_root_source(toolang_root: Path) -> SourceObservation:
    """Observe root config and capability files without reading bytes."""

    _require_source_shape(toolang_root / "config.toml", shape="file")
    for directory_name in CAP_DIRECTORY_NAMES:
        _require_source_shape(toolang_root / directory_name, shape="directory")
    return observe_source(toolang_root, ("config.toml", *CAP_DIRECTORY_NAMES))


def observe_home_source(toolang_root: Path, agent_name: str) -> SourceObservation:
    """Observe one agent home's State files without reading bytes."""

    home = toolang_root / "agents" / agent_name
    for name in ("agent.too", "config.toml"):
        _require_source_shape(home / name, shape="file")
    for directory_name in ("flows", *CAP_DIRECTORY_NAMES):
        _require_source_shape(home / directory_name, shape="directory")
    return observe_source(home, _home_source_paths(home))


def root_source_manifest(
    observation: SourceObservation,
    *,
    previous_observation: SourceObservation | None = None,
    previous_manifest: SourceManifest | None = None,
    invalidated: Collection[str] = (),
) -> SourceManifest:
    return build_source_manifest(
        observation,
        project_configs=True,
        previous_observation=previous_observation,
        previous_manifest=previous_manifest,
        invalidated=invalidated,
    )


def home_source_manifest(
    observation: SourceObservation,
    *,
    previous_observation: SourceObservation | None = None,
    previous_manifest: SourceManifest | None = None,
    invalidated: Collection[str] = (),
) -> SourceManifest:
    return build_source_manifest(
        observation,
        project_configs=True,
        previous_observation=previous_observation,
        previous_manifest=previous_manifest,
        invalidated=invalidated,
    )


def _home_source_paths(home: Path) -> tuple[str, ...]:
    return (
        "agent.too",
        "config.toml",
        *CAP_DIRECTORY_NAMES,
        *(
            (Path("flows") / path.name).as_posix()
            for path in _direct_flow_files(home / "flows")
        ),
    )


def _selected_files(path: Path) -> tuple[Path, ...]:
    if path.is_symlink() and path.is_dir():
        raise ValueError(f"source does not support symbolic-link directories: {path}")
    if path.is_file():
        return (path,)
    if not path.is_dir():
        raise ValueError(f"unsupported source node: {path}")
    files: list[Path] = []
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        files.extend(_selected_files(child))
    return tuple(files)


def _observe_file(path: Path, *, relative_path: str) -> SourceObservationEntry:
    target = path.stat()
    if not path.is_symlink():
        return SourceObservationEntry(
            path=relative_path,
            source=path,
            device=target.st_dev,
            inode=target.st_ino,
            mtime_ns=target.st_mtime_ns,
            size=target.st_size,
        )
    link = path.lstat()
    return SourceObservationEntry(
        path=relative_path,
        source=path,
        device=target.st_dev,
        inode=target.st_ino,
        mtime_ns=target.st_mtime_ns,
        size=target.st_size,
        link_device=link.st_dev,
        link_inode=link.st_ino,
        link_mtime_ns=link.st_mtime_ns,
        link_size=link.st_size,
        link_target=os.readlink(path),
    )


def _portable_relative_path(value: str) -> Path:
    path = Path(value)
    if (
        not value
        or not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or path.as_posix() != value
    ):
        raise ValueError(f"source path must be portable and relative: {value!r}")
    return path


def _require_source_shape(
    path: Path,
    *,
    shape: Literal["file", "directory"],
) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if shape == "directory" and path.is_symlink():
        raise ValueError(f"source does not support symbolic-link directories: {path}")
    matches = path.is_file() if shape == "file" else path.is_dir()
    if not matches:
        raise ValueError(f"source must be a {shape}: {path}")


def _integer_field(data: Mapping[str, object], key: str) -> int:
    value = data[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"source field {key!r} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class ProgramSource:
    """Authored program text captured during preparation."""

    agent_name: str
    kind: Literal["agent", "flow"]
    authored_path: str
    source_path: str
    source_text: str
    digest: str

    def parse(self) -> Program:
        source = _parseable_program_source(self.source_text)
        return (
            Program.from_source(source)
            if source.strip()
            else Program(span=Span(line=1))
        )


@dataclass(frozen=True, slots=True)
class SourceFile:
    """One authored file captured with fixed content."""

    path: Path
    relative_path: str
    category: str
    origin: str
    content: bytes
    digest: str
    size: int

    def read_text(self) -> str:
        return self.content.decode("utf-8")


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """Fixed authored files read while preparing one scope."""

    toolang_root: Path
    agent_name: str
    files: tuple[SourceFile, ...]

    @property
    def program_path(self) -> str | None:
        expected = Path("agents") / self.agent_name / "agent.too"
        for item in self.files:
            if item.category == "program" and Path(item.relative_path) == expected:
                return item.relative_path
        return None

    @property
    def program_files(self) -> tuple[SourceFile, ...]:
        """Return agent and direct flow program files in authored-path order."""

        return tuple(item for item in self.files if item.category == "program")

    def load_program(self) -> ProgramSource:
        expected = Path("agents") / self.agent_name / "agent.too"
        program_file = next(
            (
                item
                for item in self.program_files
                if Path(item.relative_path) == expected
            ),
            None,
        )
        source_path = (
            program_file.relative_path
            if program_file is not None
            else f"agents/{self.agent_name}/agent.too"
        )
        source = ProgramSource(
            agent_name=self.agent_name,
            kind="agent",
            authored_path="agent.too",
            source_path=source_path,
            source_text=(
                program_file.read_text()
                if program_file is not None
                else f"agent {self.agent_name}\n"
            ),
            digest=(
                program_file.digest
                if program_file is not None
                else sha256(f"agent {self.agent_name}\n".encode()).hexdigest()
            ),
        )
        return source

    def load_programs(self) -> tuple[ProgramSource, ...]:
        """Load the special agent module followed by direct home flow modules."""

        agent = self.load_program()
        prefix = Path("agents") / self.agent_name
        flows = tuple(
            ProgramSource(
                agent_name=self.agent_name,
                kind="flow",
                authored_path=Path(item.relative_path).relative_to(prefix).as_posix(),
                source_path=item.relative_path,
                source_text=item.read_text(),
                digest=item.digest,
            )
            for item in self.program_files
            if Path(item.relative_path) != prefix / "agent.too"
        )
        return (agent, *flows)

    @property
    def config_paths(self) -> tuple[str, ...]:
        return tuple(
            item.relative_path for item in self.files if item.category == "config"
        )


def source_manifest_from_snapshot(
    snapshot: SourceSnapshot,
    *,
    scope: Literal["root", "home"],
) -> SourceManifest:
    """Build one scope manifest from already captured canonical file contents."""

    prefix = Path("agents") / snapshot.agent_name
    entries: list[SourceManifestEntry] = []
    for item in snapshot.files:
        if item.origin != ("root" if scope == "root" else "agent"):
            continue
        path = Path(item.relative_path)
        if scope == "home":
            try:
                path = path.relative_to(prefix)
            except ValueError as exc:
                raise ValueError(
                    f"home source is outside the agent directory: {item.relative_path}"
                ) from exc
        entries.append(
            SourceManifestEntry(
                path=path.as_posix(),
                size=item.size,
                digest=item.digest,
            )
        )
    return SourceManifest(files=tuple(sorted(entries, key=lambda item: item.path)))


def read_authored_source(toolang_root: Path, agent_name: str) -> SourceSnapshot:
    """Read root and agent-home authored files with fixed contents."""

    files = tuple(
        sorted(
            _authored_files(toolang_root, agent_name),
            key=lambda item: item.relative_path,
        )
    )
    return _authored_source(toolang_root, agent_name, files)


def read_root_source(toolang_root: Path) -> SourceSnapshot:
    """Read only root-authored files with fixed contents."""

    files = tuple(
        sorted(_root_authored_files(toolang_root), key=lambda item: item.relative_path)
    )
    return _authored_source(toolang_root, "", files)


def is_source_path(toolang_root: Path, agent_name: str, path: Path) -> bool:
    """Return whether one path contributes to a root or home State layer."""

    return source_path_scope(toolang_root, agent_name, path) is not None


def source_path_scope(
    toolang_root: Path,
    agent_name: str,
    path: Path,
) -> tuple[Literal["root", "home"], str] | None:
    """Return the State scope and logical relative path for one source path."""

    relative_path = _relative_to_root(toolang_root, path)
    if relative_path is None:
        return None
    if relative_path == Path("config.toml"):
        return "root", relative_path.as_posix()
    if relative_path.parts[:1] and relative_path.parts[0] in CAP_DIRECTORY_NAMES:
        return (
            ("root", relative_path.as_posix())
            if len(relative_path.parts) >= 2
            else None
        )
    if relative_path.parts[:2] != ("agents", agent_name):
        return None
    agent_relative = Path(*relative_path.parts[2:])
    if agent_relative in {Path("config.toml"), Path("agent.too")}:
        return "home", agent_relative.as_posix()
    if (
        len(agent_relative.parts) == 2
        and agent_relative.parts[0] == "flows"
        and agent_relative.suffix == ".too"
    ):
        return "home", agent_relative.as_posix()
    if (
        agent_relative.parts
        and agent_relative.parts[0] in CAP_DIRECTORY_NAMES
        and len(agent_relative.parts) >= 2
    ):
        return "home", agent_relative.as_posix()
    return None


def _parseable_program_source(source_text: str) -> str:
    lines = source_text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _AGENT_HEADER_RE.match(line.strip()):
            lines[index] = ""
        break
    rendered = "\n".join(lines)
    return f"{rendered}\n" if source_text.endswith("\n") else rendered


def _authored_source(
    toolang_root: Path,
    agent_name: str,
    files: tuple[SourceFile, ...],
) -> SourceSnapshot:
    return SourceSnapshot(
        toolang_root=toolang_root,
        agent_name=agent_name,
        files=files,
    )


def _authored_files(toolang_root: Path, agent_name: str) -> list[SourceFile]:
    agent_dir = toolang_root / "agents" / agent_name
    files = _root_authored_files(toolang_root)
    files.extend(
        _collect_file(
            toolang_root, agent_dir / "config.toml", category="config", origin="agent"
        )
    )
    for path in _direct_flow_files(agent_dir / "flows"):
        files.extend(
            _collect_file(
                toolang_root,
                path,
                category="program",
                origin="agent",
            )
        )
    files.extend(
        _collect_file(
            toolang_root,
            agent_dir / "agent.too",
            category="program",
            origin="agent",
        )
    )
    for directory_name in CAP_DIRECTORY_NAMES:
        files.extend(
            _collect_directory(
                toolang_root, agent_dir / directory_name, category="cap", origin="agent"
            )
        )
    return files


def _root_authored_files(toolang_root: Path) -> list[SourceFile]:
    files = _collect_file(
        toolang_root,
        toolang_root / "config.toml",
        category="config",
        origin="root",
    )
    for directory_name in CAP_DIRECTORY_NAMES:
        files.extend(
            _collect_directory(
                toolang_root,
                toolang_root / directory_name,
                category="cap",
                origin="root",
            )
        )
    return files


def _collect_file(
    toolang_root: Path,
    path: Path,
    *,
    category: str,
    origin: str,
) -> list[SourceFile]:
    if not path.exists() and not path.is_symlink():
        return []
    _require_source_shape(path, shape="file")
    before = _observe_file(
        path,
        relative_path=path.relative_to(toolang_root).as_posix(),
    )
    content = path.read_bytes()
    if category == "config":
        content = canonical_state_config(content)
    after = _observe_file(path, relative_path=before.path)
    if before != after:
        raise SourceChangedError(f"source changed while reading: {path}")
    if category == "config" and not content:
        return []
    return [
        SourceFile(
            path=path,
            relative_path=path.relative_to(toolang_root).as_posix(),
            category=category,
            origin=origin,
            content=content,
            digest=sha256(content).hexdigest(),
            size=len(content),
        )
    ]


def _collect_directory(
    toolang_root: Path,
    directory: Path,
    *,
    category: str,
    origin: str,
) -> list[SourceFile]:
    if not directory.exists() and not directory.is_symlink():
        return []
    _require_source_shape(directory, shape="directory")
    files: list[SourceFile] = []
    for path in _selected_files(directory):
        files.extend(
            _collect_file(toolang_root, path, category=category, origin=origin)
        )
    return files


def _direct_flow_files(directory: Path) -> tuple[Path, ...]:
    if not directory.exists() and not directory.is_symlink():
        return ()
    _require_source_shape(directory, shape="directory")
    return tuple(
        path
        for path in sorted(directory.iterdir(), key=lambda item: item.name)
        if path.is_file() and path.suffix == ".too"
    )


def _relative_to_root(toolang_root: Path, path: Path) -> Path | None:
    try:
        return path.absolute().relative_to(toolang_root.absolute())
    except ValueError:
        return None
