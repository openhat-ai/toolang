"""Filesystem source trees and captured authored file contents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Literal, cast
from uuid import uuid4

from toolang.catalog.types import CAP_DIRECTORY_NAMES

from ..lang.ast import Program, Span

SourceNodeKind = Literal["file", "directory"]
SOURCE_SCHEMA = 1
_AGENT_HEADER_RE = re.compile(r"^agent\s+[A-Za-z_][\w-]*\s*$")


@dataclass(frozen=True, slots=True)
class SourceNode:
    """One filesystem node in a coarse source snapshot."""

    name: str
    kind: SourceNodeKind
    mtime_ns: int
    size: int
    children: tuple[SourceNode, ...] = ()

    def to_data(self) -> dict[str, object]:
        """Return the canonical JSON-compatible node representation."""

        return {
            "name": self.name,
            "type": self.kind,
            "mtime_ns": self.mtime_ns,
            "size": self.size,
            "children": [child.to_data() for child in self.children],
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> SourceNode:
        """Load one source node from JSON-compatible data."""

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
            children=children,
        )


@dataclass(frozen=True, slots=True)
class Source:
    """A JSON-persisted metadata snapshot of selected source paths."""

    root: SourceNode
    schema: int = SOURCE_SCHEMA

    def to_data(self) -> dict[str, object]:
        """Return the canonical JSON-compatible tree representation."""

        return {"schema": self.schema, "root": self.root.to_data()}

    def save(self, path: Path) -> None:
        """Atomically save this source tree as formatted JSON."""

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
        temporary.write_text(
            json.dumps(self.to_data(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    @classmethod
    def from_data(cls, data: dict[str, object]) -> Source:
        """Load a source tree from JSON-compatible data."""

        schema = _integer_field(data, "schema")
        if schema != SOURCE_SCHEMA:
            raise ValueError(f"unsupported source schema: {schema}")
        return cls(
            schema=schema,
            root=SourceNode.from_data(cast(dict[str, object], data["root"])),
        )

    @classmethod
    def load(cls, path: Path) -> Source:
        """Load a source tree from one JSON file."""

        data = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        return cls.from_data(data)


def scan_source(base: Path, paths: tuple[str, ...]) -> Source:
    """Capture selected paths below one base without reading file contents."""

    children: list[SourceNode] = []
    for value in sorted(set(paths)):
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"source path must be relative to its base: {value!r}")
        path = base / relative
        if not path.exists():
            continue
        children.append(_scan_node(path, name=value))
    return Source(
        root=SourceNode(
            name=".",
            kind="directory",
            mtime_ns=0,
            size=0,
            children=tuple(children),
        )
    )


def scan_root_source(toolang_root: Path) -> Source:
    """Capture root config and authored cap paths."""

    return scan_source(
        toolang_root,
        ("config.toml", *CAP_DIRECTORY_NAMES),
    )


def scan_home_source(toolang_root: Path, agent_name: str) -> Source:
    """Capture one agent program, config, and authored cap paths."""

    return scan_source(
        toolang_root / "agents" / agent_name,
        ("agent.too", "config.toml", *CAP_DIRECTORY_NAMES),
    )


def _scan_node(path: Path, *, name: str | None = None) -> SourceNode:
    if path.is_symlink() and path.is_dir():
        raise ValueError(
            f"source tree does not support symbolic-link directories: {path}"
        )
    stat = path.stat()
    node_name = name if name is not None else path.name
    if path.is_file():
        return SourceNode(
            name=node_name,
            kind="file",
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
        )
    if not path.is_dir():
        raise ValueError(f"unsupported source node: {path}")
    children = tuple(
        _scan_node(child)
        for child in sorted(path.iterdir(), key=lambda item: item.name)
    )
    return SourceNode(
        name=node_name,
        kind="directory",
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        children=children,
    )


def _integer_field(data: dict[str, object], key: str) -> int:
    value = data[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"source field {key!r} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class ProgramSource:
    """Authored program text captured during preparation."""

    agent_name: str
    source_path: str
    source_text: str

    def parse(self) -> Program:
        source = _parseable_program_source(self.source_text)
        return (
            Program.from_source(source)
            if source.strip()
            else Program(span=Span(line=1))
        )


@dataclass(frozen=True, slots=True)
class AuthoredFile:
    """One authored file captured with fixed content."""

    path: Path
    relative_path: str
    category: str
    origin: str
    content: bytes
    digest: str
    mtime_ns: int
    size: int

    def read_text(self) -> str:
        return self.content.decode("utf-8")


@dataclass(frozen=True, slots=True)
class AuthoredSource:
    """Fixed authored files read while preparing one scope."""

    toolang_root: Path
    agent_name: str
    files: tuple[AuthoredFile, ...]
    fingerprint: str
    scanned_at: str

    @property
    def program_path(self) -> str | None:
        for item in self.files:
            if item.category == "program":
                return item.relative_path
        return None

    def load_program(self) -> ProgramSource:
        program_file = next(
            (item for item in self.files if item.category == "program"),
            None,
        )
        source_path = (
            program_file.relative_path
            if program_file is not None
            else f"agents/{self.agent_name}/agent.too"
        )
        source = ProgramSource(
            agent_name=self.agent_name,
            source_path=source_path,
            source_text=(
                program_file.read_text()
                if program_file is not None
                else f"agent {self.agent_name}\n"
            ),
        )
        source.parse()
        return source

    @property
    def config_paths(self) -> tuple[str, ...]:
        return tuple(
            item.relative_path for item in self.files if item.category == "config"
        )


def read_authored_source(toolang_root: Path, agent_name: str) -> AuthoredSource:
    """Read root and agent-home authored files with fixed contents."""

    files = tuple(
        sorted(
            _authored_files(toolang_root, agent_name),
            key=lambda item: item.relative_path,
        )
    )
    return _authored_source(toolang_root, agent_name, files)


def read_root_source(toolang_root: Path) -> AuthoredSource:
    """Read only root-authored files with fixed contents."""

    files = tuple(
        sorted(_root_authored_files(toolang_root), key=lambda item: item.relative_path)
    )
    return _authored_source(toolang_root, "", files)


def is_source_path(toolang_root: Path, agent_name: str, path: Path) -> bool:
    """Return whether one path contributes to root or agent prepared state."""

    relative_path = _relative_to_root(toolang_root, path)
    if relative_path is None:
        return False
    if relative_path == Path("config.toml"):
        return True
    if relative_path.parts[:1] and relative_path.parts[0] in CAP_DIRECTORY_NAMES:
        return len(relative_path.parts) >= 2
    if relative_path.parts[:2] != ("agents", agent_name):
        return False
    agent_relative = Path(*relative_path.parts[2:])
    if agent_relative in {Path("config.toml"), Path("agent.too")}:
        return True
    return bool(
        agent_relative.parts
        and agent_relative.parts[0] in CAP_DIRECTORY_NAMES
        and len(agent_relative.parts) >= 2
    )


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
    files: tuple[AuthoredFile, ...],
) -> AuthoredSource:
    return AuthoredSource(
        toolang_root=toolang_root,
        agent_name=agent_name,
        files=files,
        fingerprint=_content_fingerprint(files),
        scanned_at=datetime.now(timezone.utc).isoformat(),
    )


def _authored_files(toolang_root: Path, agent_name: str) -> list[AuthoredFile]:
    agent_dir = toolang_root / "agents" / agent_name
    files = _root_authored_files(toolang_root)
    files.extend(
        _collect_file(
            toolang_root, agent_dir / "config.toml", category="config", origin="agent"
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


def _root_authored_files(toolang_root: Path) -> list[AuthoredFile]:
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
) -> list[AuthoredFile]:
    if not path.is_file():
        return []
    content = path.read_bytes()
    stat = path.stat()
    return [
        AuthoredFile(
            path=path,
            relative_path=str(path.relative_to(toolang_root)),
            category=category,
            origin=origin,
            content=content,
            digest=sha256(content).hexdigest(),
            mtime_ns=stat.st_mtime_ns,
            size=len(content),
        )
    ]


def _collect_directory(
    toolang_root: Path,
    directory: Path,
    *,
    category: str,
    origin: str,
) -> list[AuthoredFile]:
    if not directory.exists():
        return []
    files: list[AuthoredFile] = []
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        files.extend(
            _collect_file(toolang_root, path, category=category, origin=origin)
        )
    return files


def _content_fingerprint(files: tuple[AuthoredFile, ...]) -> str:
    digest = sha256()
    for item in files:
        digest.update(item.relative_path.encode("utf-8"))
        digest.update(item.digest.encode("utf-8"))
        digest.update(str(item.mtime_ns).encode("utf-8"))
        digest.update(str(item.size).encode("utf-8"))
    return digest.hexdigest()


def _relative_to_root(toolang_root: Path, path: Path) -> Path | None:
    try:
        return path.resolve(strict=False).relative_to(
            toolang_root.resolve(strict=False)
        )
    except ValueError:
        return None
