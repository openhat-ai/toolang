"""Durable authored-file scanning and change detection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re

from ..lang.ast import Program, Span

CAP_DIR_NAMES = ("psyches", "skills", "services", "prompts")
_AGENT_HEADER_RE = re.compile(r"^agent\s+[A-Za-z_][\w-]*\s*$")


@dataclass(frozen=True, slots=True)
class ProgramSource:
    """Authored program source captured in durable state."""

    agent_name: str
    source_path: str
    source_text: str

    def parse(self) -> Program:
        source = _parseable_program_source(self.source_text)
        return Program.from_source(source) if source.strip() else Program(span=Span(line=1))

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

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        agent_name: str,
        source_path: str,
    ) -> "ProgramSource":
        """Load and validate one explicitly located program source."""

        source_text = (
            path.read_text(encoding="utf-8")
            if path.is_file()
            else f"agent {agent_name}\n"
        )
        source = cls(
            agent_name=agent_name,
            source_path=source_path,
            source_text=source_text,
        )
        source.parse()
        return source


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
    """One durable authored file."""

    path: Path
    relative_path: str
    category: str
    origin: str
    digest: str
    mtime_ns: int
    size: int


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

        source_path = self.program_path or f"agents/{self.agent_name}/agent.too"
        return ProgramSource.load(
            self.toolang_root / source_path,
            agent_name=self.agent_name,
            source_path=source_path,
        )

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


def is_durable_path(toolang_root: Path, agent_name: str, path: Path) -> bool:
    """Return whether one path belongs to durable authored state."""

    relative_path = _relative_to_root(toolang_root, path)
    if relative_path is None:
        return False
    if relative_path == Path("config.toml"):
        return True
    if relative_path.parts[:1] and relative_path.parts[0] in CAP_DIR_NAMES:
        return len(relative_path.parts) >= 2
    if relative_path.parts[:2] != ("agents", agent_name):
        return False
    agent_relative = Path(*relative_path.parts[2:])
    if agent_relative in {Path("config.toml"), Path("agent.too")}:
        return True
    if not agent_relative.parts:
        return False
    return agent_relative.parts[0] in CAP_DIR_NAMES and len(agent_relative.parts) >= 2


def _durable_files(toolang_root: Path, agent_name: str) -> list[DurableFile]:
    agent_dir = toolang_root / "agents" / agent_name
    files: list[DurableFile] = []
    files.extend(
        _collect_file(
            toolang_root, toolang_root / "config.toml", category="config", origin="root"
        )
    )
    for directory_name in CAP_DIR_NAMES:
        files.extend(
            _collect_directory(
                toolang_root,
                toolang_root / directory_name,
                category="cap",
                origin="root",
            )
        )
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
    for directory_name in CAP_DIR_NAMES:
        files.extend(
            _collect_directory(
                toolang_root, agent_dir / directory_name, category="cap", origin="agent"
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
    stat = path.stat()
    return [
        DurableFile(
            path=path,
            relative_path=str(path.relative_to(toolang_root)),
            category=category,
            origin=origin,
            digest=sha256(path.read_bytes()).hexdigest(),
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
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
