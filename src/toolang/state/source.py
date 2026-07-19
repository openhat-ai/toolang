"""Filesystem-only source tree snapshots for prepared state."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from toolang.catalog.cap import CAP_DIRECTORY_NAMES

SourceNodeKind = Literal["file", "directory"]
SOURCE_SCHEMA = 1


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
class SourceTree:
    """A JSON-persisted metadata snapshot of selected source paths."""

    root: SourceNode
    schema: int = SOURCE_SCHEMA

    def to_data(self) -> dict[str, object]:
        """Return the canonical JSON-compatible tree representation."""

        return {"schema": self.schema, "root": self.root.to_data()}

    def canonical_bytes(self) -> bytes:
        """Return stable bytes used by prepared version calculation."""

        return json.dumps(
            self.to_data(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

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
    def from_data(cls, data: dict[str, object]) -> SourceTree:
        """Load a source tree from JSON-compatible data."""

        schema = _integer_field(data, "schema")
        if schema != SOURCE_SCHEMA:
            raise ValueError(f"unsupported source schema: {schema}")
        return cls(
            schema=schema,
            root=SourceNode.from_data(cast(dict[str, object], data["root"])),
        )

    @classmethod
    def load(cls, path: Path) -> SourceTree:
        """Load a source tree from one JSON file."""

        data = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        return cls.from_data(data)


def scan_source_tree(base: Path, paths: tuple[str, ...]) -> SourceTree:
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
    return SourceTree(
        root=SourceNode(
            name=".",
            kind="directory",
            mtime_ns=0,
            size=0,
            children=tuple(children),
        )
    )


def scan_root_source(toolang_root: Path) -> SourceTree:
    """Capture root config and authored cap paths."""

    return scan_source_tree(
        toolang_root,
        ("config.toml", *CAP_DIRECTORY_NAMES),
    )


def scan_home_source(toolang_root: Path, agent_name: str) -> SourceTree:
    """Capture one agent program, config, and authored cap paths."""

    return scan_source_tree(
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
