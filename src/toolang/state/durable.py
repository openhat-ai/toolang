"""Durable authored-file scanning and change detection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re

from toolang.catalog.cap import CAP_DIRECTORY_NAMES

from ..lang.ast import Program, Span

_AGENT_HEADER_RE = re.compile(r"^agent\s+[A-Za-z_][\w-]*\s*$")


@dataclass(frozen=True, slots=True)
class ProgramSource:
    """Authored program source captured in durable state."""

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

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "agent_name": self.agent_name,
                "source_path": self.source_path,
                "source_text": self.source_text,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(payload.encode("utf-8")).hexdigest()


def _parseable_program_source(source_text: str) -> str:
    """Hide only the agent header while retaining all authored source lines."""

    lines = source_text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _AGENT_HEADER_RE.match(line.strip()):
            lines[index] = ""
        break
    rendered = "\n".join(lines)
    return f"{rendered}\n" if source_text.endswith("\n") else rendered


@dataclass(frozen=True, slots=True)
class DurableFile:
    """One authored file captured in a durable source snapshot."""

    path: Path
    relative_path: str
    category: str
    origin: str
    content: bytes
    digest: str
    mtime_ns: int
    size: int

    def read_text(self) -> str:
        """Decode the captured file content as UTF-8 text."""

        return self.content.decode("utf-8")


@dataclass(frozen=True, slots=True)
class DurableState:
    """One durable authored-state snapshot."""

    toolang_root: Path
    agent_name: str
    files: tuple[DurableFile, ...]
    fingerprint: str
    scanned_at: str

    @property
    def program_path(self) -> str | None:
        for item in self.files:
            if item.category == "program":
                return item.relative_path
        return None

    def load_program(self) -> ProgramSource:
        """Load the program captured by this authored-file snapshot."""

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


def scan_durable_state(toolang_root: Path, agent_name: str) -> DurableState:
    """Scan durable authored files for one agent."""

    files = tuple(
        sorted(
            _durable_files(toolang_root, agent_name),
            key=lambda item: item.relative_path,
        )
    )
    return DurableState(
        toolang_root=toolang_root,
        agent_name=agent_name,
        files=files,
        fingerprint=_fingerprint(files),
        scanned_at=datetime.now(timezone.utc).isoformat(),
    )


def scan_root_durable_state(toolang_root: Path) -> DurableState:
    """Capture only root-authored files for shared preparation."""

    files = tuple(
        sorted(_root_durable_files(toolang_root), key=lambda item: item.relative_path)
    )
    return DurableState(
        toolang_root=toolang_root,
        agent_name="",
        files=files,
        fingerprint=_fingerprint(files),
        scanned_at=datetime.now(timezone.utc).isoformat(),
    )


def is_durable_path(toolang_root: Path, agent_name: str, path: Path) -> bool:
    """Return whether one path belongs to durable authored state."""

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
    if not agent_relative.parts:
        return False
    return (
        agent_relative.parts[0] in CAP_DIRECTORY_NAMES
        and len(agent_relative.parts) >= 2
    )


def _durable_files(toolang_root: Path, agent_name: str) -> list[DurableFile]:
    agent_dir = toolang_root / "agents" / agent_name
    files = _root_durable_files(toolang_root)
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


def _root_durable_files(toolang_root: Path) -> list[DurableFile]:
    files: list[DurableFile] = []
    files.extend(
        _collect_file(
            toolang_root, toolang_root / "config.toml", category="config", origin="root"
        )
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
) -> list[DurableFile]:
    if not path.is_file():
        return []
    content = path.read_bytes()
    stat = path.stat()
    return [
        DurableFile(
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
) -> list[DurableFile]:
    if not directory.exists():
        return []
    files: list[DurableFile] = []
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        files.extend(
            _collect_file(toolang_root, path, category=category, origin=origin)
        )
    return files


def _fingerprint(files: tuple[DurableFile, ...]) -> str:
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
