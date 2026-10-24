"""Portable source manifests and captured authored file contents."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from collections import OrderedDict
from dataclasses import dataclass, replace
from hashlib import sha256
import os
from pathlib import Path
import re
from threading import RLock
from typing import Literal

from toolang.catalog.types import CAP_DIRECTORY_NAMES

from ..lang.ast import AgicDecl, FlowDecl, Program
from ..common.progress import ProgressSink, emit_progress

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AGENT_PROGRAMS: OrderedDict[tuple[Path, str], Program] = OrderedDict()
_AGENT_PROGRAMS_LOCK = RLock()
ROOT_WORKSPACE_SOURCE = "root/config.toml"


def cached_agent_program(path: Path, digest: str) -> Program | None:
    """Reuse only the exact agent source validated during remote acquisition."""

    with _AGENT_PROGRAMS_LOCK:
        return _AGENT_PROGRAMS.get((path, digest))


def parse_agent_program(
    path: Path,
    source: str,
    *,
    ref: str,
    progress: ProgressSink | None = None,
    prepared: Program | None = None,
    progress_id: str | None = None,
) -> Program:
    """Validate a remote agent before dependency work and retain its exact AST."""

    digest = sha256(source.encode("utf-8")).hexdigest()
    item_id = progress_id or f"agent:{path}"
    with _AGENT_PROGRAMS_LOCK:
        existing = cached_agent_program(path, digest)
        if existing is not None:
            return existing
        if prepared is None:
            emit_progress(
                progress,
                id=item_id,
                kind="prepare",
                stage="parse",
                label="Parsing agent...",
                status="running",
                subject="agent",
                ref=ref,
            )
            try:
                prepared = Program.from_source(source)
            except Exception as exc:
                emit_progress(
                    progress,
                    id=item_id,
                    kind="prepare",
                    stage="parse",
                    label="Failed to parse agent",
                    status="failed",
                    detail=str(exc),
                    subject="agent",
                    ref=ref,
                    complete=True,
                )
                raise
            emit_progress(
                progress,
                id=item_id,
                kind="prepare",
                stage="parse",
                label="Parsed agent",
                status="ok",
                subject="agent",
                ref=ref,
                complete=True,
            )
        _AGENT_PROGRAMS[(path, digest)] = prepared
        while len(_AGENT_PROGRAMS) > 32:
            _AGENT_PROGRAMS.popitem(last=False)
        return prepared


@dataclass(frozen=True, slots=True)
class SourceIssue:
    """Stable resource-local observation failure, relative to its scope."""

    path: str
    code: str
    message: str


def source_issue(path: str, error: Exception) -> SourceIssue:
    message = (
        f"Could not read source ({type(error).__name__})"
        if isinstance(error, OSError)
        else re.sub(r": /[^\n]*$", "", str(error))
    )
    return SourceIssue(path, "source-read", message)


@dataclass(frozen=True, slots=True)
class SourceObservationEntry:
    """Process-local stat identity for one selected logical source file."""

    path: str
    source: Path
    device: int
    inode: int
    mtime_ns: int
    size: int
    mode: int = 0
    link_device: int | None = None
    link_inode: int | None = None
    link_mtime_ns: int | None = None
    link_size: int | None = None
    link_target: str | None = None
    workspace_key: str | None = None


@dataclass(frozen=True, slots=True)
class SourceObservation:
    """Process-local selected paths, listings, and inexpensive stat facts."""

    files: tuple[SourceObservationEntry, ...]
    issues: tuple[SourceIssue, ...] = ()

    def __post_init__(self) -> None:
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("source observation paths must be sorted and unique")


@dataclass(frozen=True, slots=True)
class SourceManifestEntry:
    """Captured and raw content identities for one selected source file."""

    path: str
    size: int
    digest: str
    raw_digest: str | None = None

    def __post_init__(self) -> None:
        _portable_relative_path(self.path)
        if self.size < 0:
            raise ValueError("source manifest size must be non-negative")
        if _SHA256_RE.fullmatch(self.digest) is None:
            raise ValueError("source manifest digest must be a SHA-256 hex value")
        if (
            self.raw_digest is not None
            and _SHA256_RE.fullmatch(self.raw_digest) is None
        ):
            raise ValueError("raw source digest must be a SHA-256 hex value")


@dataclass(frozen=True, slots=True)
class SourceManifest:
    """Portable content identity of every selected file in one State scope."""

    files: tuple[SourceManifestEntry, ...]
    issues: tuple[SourceIssue, ...] = ()

    def __post_init__(self) -> None:
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("source manifest paths must be sorted and unique")
        if any(item.raw_digest is None for item in self.files):
            raise ValueError("current source manifest requires raw file digests")


class SourceChangedError(RuntimeError):
    """The selected source changed while its manifest was being captured."""


def scan_source(
    base: Path,
    paths: tuple[str, ...],
) -> SourceManifest:
    """Read and hash one complete portable source manifest."""

    observation = observe_source(base, paths)
    return build_source_manifest(observation)


def scan_root_source(place: Path) -> SourceManifest:
    """Capture root config and authored cap paths."""

    return root_source_manifest(observe_root_source(place))


def scan_home_source(
    place: Path,
    agent_name: str,
    *,
    root_config: Path | None = None,
    workspace_key: str | None = None,
) -> SourceManifest:
    """Capture one agent program, config, and authored cap paths."""

    return home_source_manifest(
        observe_home_source(
            place, agent_name, root_config=root_config, workspace_key=workspace_key
        )
    )


def observe_source(
    base: Path, paths: Sequence[str], *, isolate: bool = False
) -> SourceObservation:
    """Observe logical sources; isolate resource failures when requested."""

    selected: dict[str, Path] = {}
    issues: list[SourceIssue] = []

    def visit(path: Path) -> None:
        logical = path.relative_to(base).as_posix()
        try:
            if not path.exists() and not path.is_symlink():
                return
            if path.is_symlink() and path.is_dir():
                raise ValueError("source does not support symbolic-link directories")
            if (
                logical in {"runnables", "chores", *CAP_DIRECTORY_NAMES}
                and not path.is_dir()
            ):
                raise ValueError("source must be a directory")
            if logical in {"agent.too", "config.toml"} and not path.is_file():
                raise ValueError("source must be a file")
            if path.is_file():
                selected[logical] = path
                return
            if not path.is_dir():
                raise ValueError("unsupported source node")
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                if logical in {"runnables", "chores"} and (
                    child.suffix != (".too" if logical == "runnables" else ".md")
                    or child.is_dir()
                ):
                    continue
                visit(child)
        except (OSError, ValueError) as exc:
            if not isolate or logical == "config.toml":
                raise
            issues.append(source_issue(logical, exc))

    for value in sorted(set(paths)):
        visit(base / _portable_relative_path(value))
    files: list[SourceObservationEntry] = []
    for logical, path in sorted(selected.items()):
        try:
            files.append(_observe_file(path, relative_path=logical))
        except OSError as exc:
            if not isolate or logical == "config.toml":
                raise
            issues.append(source_issue(logical, exc))
    return SourceObservation(
        tuple(files), tuple(sorted(issues, key=lambda item: item.path))
    )


def build_source_manifest(
    observation: SourceObservation,
    *,
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
    issues = list(observation.issues)
    for item in observation.files:
        if (
            item.path not in invalidated_paths
            and previous_observed.get(item.path) == item
            and previous_manifest is not None
        ):
            cached = previous_files.get(item.path)
            if cached is not None and cached.raw_digest is not None:
                entries.append(cached)
                continue
        try:
            content = item.source.read_bytes()
        except OSError as exc:
            if item.path == "config.toml":
                raise
            issues.append(source_issue(item.path, exc))
            continue
        if (
            replace(
                _observe_file(item.source, relative_path=item.path),
                workspace_key=item.workspace_key,
            )
            != item
        ):
            raise SourceChangedError(f"source changed while reading: {item.source}")
        if item.path == ROOT_WORKSPACE_SOURCE:
            from .config import root_workspace_content

            content = root_workspace_content(
                content, item.workspace_key, item.source.parent
            )
            if not content:
                continue
        raw_digest = sha256(content).hexdigest()
        entries.append(
            SourceManifestEntry(
                path=item.path,
                size=len(content),
                digest=raw_digest,
                raw_digest=raw_digest,
            )
        )
    return SourceManifest(
        files=tuple(entries), issues=tuple(sorted(issues, key=lambda item: item.path))
    )


def observe_root_source(place: Path) -> SourceObservation:
    """Observe shared config and independently authored capabilities."""
    _require_source_shape(place / "config.toml", shape="file")
    return observe_source(place, ("config.toml", *CAP_DIRECTORY_NAMES), isolate=True)


def observe_home_source(
    place: Path,
    agent_name: str,
    *,
    root_config: Path | None = None,
    workspace_key: str | None = None,
) -> SourceObservation:
    """Observe home sources without creating optional resource directories."""
    home = place / "agents" / agent_name
    _require_source_shape(home / "config.toml", shape="file")
    observation = observe_source(home, _home_source_paths(home), isolate=True)
    if root_config is None:
        return observation
    _require_source_shape(root_config, shape="file")
    if not root_config.exists():
        return observation
    entry = replace(
        _observe_file(root_config, relative_path=ROOT_WORKSPACE_SOURCE),
        workspace_key=workspace_key,
    )
    return SourceObservation(
        tuple(sorted((*observation.files, entry), key=lambda item: item.path)),
        observation.issues,
    )


def root_source_manifest(
    observation: SourceObservation,
    *,
    previous_observation: SourceObservation | None = None,
    previous_manifest: SourceManifest | None = None,
    invalidated: Collection[str] = (),
) -> SourceManifest:
    return build_source_manifest(
        observation,
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
        previous_observation=previous_observation,
        previous_manifest=previous_manifest,
        invalidated=invalidated,
    )


def _home_source_paths(home: Path) -> tuple[str, ...]:
    return ("agent.too", "config.toml", "runnables", "chores", *CAP_DIRECTORY_NAMES)


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
            mode=target.st_mode,
        )
    link = path.lstat()
    return SourceObservationEntry(
        path=relative_path,
        source=path,
        device=target.st_dev,
        inode=target.st_ino,
        mtime_ns=target.st_mtime_ns,
        size=target.st_size,
        mode=target.st_mode,
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


@dataclass(frozen=True, slots=True)
class ProgramSource:
    """Authored program text captured during preparation."""

    agent_name: str
    kind: Literal["agent", "runnable"]
    authored_path: str
    source_path: str
    source_text: str
    digest: str

    def parse(
        self,
        *,
        external_runnables: Mapping[str, tuple[Program, AgicDecl | FlowDecl]]
        | None = None,
    ) -> Program:
        return Program.from_source(
            self.source_text, external_runnables=external_runnables
        )


@dataclass(frozen=True, slots=True)
class SourceFile:
    """One authored file captured with fixed content."""

    path: Path
    relative_path: str
    category: str
    layer: str
    content: bytes
    digest: str
    size: int
    raw_digest: str
    raw_content: bytes | None = None

    def read_text(self) -> str:
        return self.content.decode("utf-8")


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """Fixed authored files read while preparing one scope."""

    place: Path
    agent_name: str
    files: tuple[SourceFile, ...]
    issues: tuple[SourceIssue, ...] = ()

    @property
    def program_path(self) -> str | None:
        expected = Path("agents") / self.agent_name / "agent.too"
        for item in self.files:
            if item.category == "program" and Path(item.relative_path) == expected:
                return item.relative_path
        return None

    @property
    def program_files(self) -> tuple[SourceFile, ...]:
        """Return agent and direct runnable program files in authored-path order."""

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
            source_text=program_file.read_text() if program_file is not None else "",
            digest=(
                program_file.digest
                if program_file is not None
                else sha256(b"").hexdigest()
            ),
        )
        return source

    def load_programs(self) -> tuple[ProgramSource, ...]:
        """Load the special agent module followed by direct home runnable modules."""

        agent = self.load_program()
        prefix = Path("agents") / self.agent_name
        runnables = tuple(
            ProgramSource(
                agent_name=self.agent_name,
                kind="runnable",
                authored_path=Path(item.relative_path).relative_to(prefix).as_posix(),
                source_path=item.relative_path,
                source_text=item.read_text(),
                digest=item.digest,
            )
            for item in self.program_files
            if Path(item.relative_path) != prefix / "agent.too"
        )
        return (agent, *runnables)

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
        if item.layer != ("root" if scope == "root" else "agent"):
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
                size=len(item.raw_content)
                if item.raw_content is not None
                else item.size,
                digest=item.raw_digest,
                raw_digest=item.raw_digest,
            )
        )
    issues = tuple(
        item
        for item in snapshot.issues
        if (scope == "root") == (not item.path.startswith("agents/"))
    )
    if scope == "home":
        issues = tuple(
            SourceIssue(
                Path(item.path).relative_to(prefix).as_posix(), item.code, item.message
            )
            for item in issues
        )
    return SourceManifest(
        files=tuple(sorted(entries, key=lambda item: item.path)), issues=issues
    )


def _read_scope_sources(
    place: Path,
    agent_name: str,
    *,
    include_home: bool,
    root_config: Path | None = None,
    workspace_key: str | None = None,
) -> SourceSnapshot:
    files: list[SourceFile] = []
    issues: list[SourceIssue] = []
    observations = [(place, "root", observe_root_source(place))]
    if include_home:
        observations.append(
            (
                place / "agents" / agent_name,
                "agent",
                observe_home_source(
                    place,
                    agent_name,
                    root_config=root_config,
                    workspace_key=workspace_key,
                ),
            )
        )
    for base, layer, observation in observations:
        for issue in observation.issues:
            issues.append(
                SourceIssue(
                    (base / issue.path).relative_to(place).as_posix(),
                    issue.code,
                    issue.message,
                )
            )
        for entry in observation.files:
            if layer == "agent" and entry.path == ROOT_WORKSPACE_SOURCE:
                from .config import root_workspace_content

                item = _collect_file(
                    place,
                    entry.source,
                    category="workspace-policy",
                    layer=layer,
                    relative_path=(
                        Path("agents") / agent_name / ROOT_WORKSPACE_SOURCE
                    ).as_posix(),
                )[0]
                content = root_workspace_content(
                    item.content, workspace_key, entry.source.parent
                )
                if not content:
                    continue
                files.append(
                    replace(
                        item,
                        content=content,
                        digest=sha256(content).hexdigest(),
                        size=len(content),
                        raw_content=content,
                        raw_digest=sha256(content).hexdigest(),
                    )
                )
                continue
            head = Path(entry.path).parts[0]
            category = (
                "config"
                if entry.path == "config.toml"
                else "program"
                if entry.path == "agent.too" or head == "runnables"
                else "chore"
                if head == "chores"
                else "cap"
            )
            try:
                files.extend(
                    _collect_file(place, entry.source, category=category, layer=layer)
                )
            except SourceChangedError:
                raise
            except (OSError, UnicodeError, ValueError) as exc:
                if category == "config":
                    raise
                issues.append(
                    source_issue(entry.source.relative_to(place).as_posix(), exc)
                )
    return SourceSnapshot(
        place,
        agent_name,
        tuple(sorted(files, key=lambda item: item.relative_path)),
        tuple(sorted(issues, key=lambda item: item.path)),
    )


def read_authored_source(
    place: Path,
    agent_name: str,
    *,
    root_config: Path | None = None,
    workspace_key: str | None = None,
) -> SourceSnapshot:
    """Capture current files, isolating resource-local read failures."""
    return _read_scope_sources(
        place,
        agent_name,
        include_home=True,
        root_config=root_config,
        workspace_key=workspace_key,
    )


def read_root_source(place: Path) -> SourceSnapshot:
    return _read_scope_sources(place, "", include_home=False)


def is_source_path(place: Path, agent_name: str, path: Path) -> bool:
    """Return whether one path contributes to a root or home State layer."""

    return source_path_scope(place, agent_name, path) is not None


def source_path_scope(
    place: Path,
    agent_name: str,
    path: Path,
) -> tuple[Literal["root", "home"], str] | None:
    """Return the State scope and logical relative path for one source path."""

    relative_path = _relative_to_root(place, path)
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
        and agent_relative.parts[0] in {"runnables", "chores"}
        and agent_relative.suffix
        == (".too" if agent_relative.parts[0] == "runnables" else ".md")
    ):
        return "home", agent_relative.as_posix()
    if (
        agent_relative.parts
        and agent_relative.parts[0] in CAP_DIRECTORY_NAMES
        and len(agent_relative.parts) >= 2
    ):
        return "home", agent_relative.as_posix()
    return None


def _collect_file(
    place: Path,
    path: Path,
    *,
    category: str,
    layer: str,
    relative_path: str | None = None,
) -> list[SourceFile]:
    if not path.exists() and not path.is_symlink():
        return []
    _require_source_shape(path, shape="file")
    before = _observe_file(
        path,
        relative_path=relative_path or path.relative_to(place).as_posix(),
    )
    content = path.read_bytes()
    raw_content = content
    raw_digest = sha256(content).hexdigest()
    if category == "config":
        from toolang.common.config_sources import rebase_config_content

        content = rebase_config_content(
            content, path.parent, include_workspaces=layer == "agent"
        )
    after = _observe_file(path, relative_path=before.path)
    if before != after:
        raise SourceChangedError(f"source changed while reading: {path}")
    return [
        SourceFile(
            path=path,
            relative_path=before.path,
            category=category,
            layer=layer,
            content=content,
            digest=sha256(content).hexdigest(),
            size=len(content),
            raw_digest=raw_digest,
            raw_content=raw_content,
        )
    ]


def _relative_to_root(place: Path, path: Path) -> Path | None:
    try:
        return path.absolute().relative_to(place.absolute())
    except ValueError:
        return None
