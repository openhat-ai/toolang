"""Canonical filesystem layout for one materialized agent."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Literal

AgentPlacement = Literal["resident", "visiting", "roaming"]


@dataclass(frozen=True, slots=True)
class AgentLayout:
    """Immutable paths for one agent under one materialized Toolang root."""

    root: Path
    name: str
    placement: AgentPlacement

    def __post_init__(self) -> None:
        root = self.root.expanduser().resolve()
        name = self.name.strip()
        if self.placement not in {"resident", "visiting", "roaming"}:
            raise ValueError(f"invalid agent placement: {self.placement!r}")
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ValueError(f"invalid agent name: {self.name!r}")
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "name", name)

    @classmethod
    def resident(cls, root: Path, name: str) -> AgentLayout:
        """Create a resident-agent layout under an explicit Toolang root."""

        return cls(root=root, name=name, placement="resident")

    @classmethod
    def visiting(cls, source: str, name: str) -> AgentLayout:
        """Create the stable temporary layout for one remote source selector."""

        label = _safe_agent_label(name)
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:8]
        return cls(
            root=Path("/tmp") / f"toolang-{label}-{digest}",
            name=name,
            placement="visiting",
        )

    @classmethod
    def roaming(cls, source: Path) -> AgentLayout:
        """Create the source-local layout for one roaming program."""

        source = source.expanduser().resolve()
        return cls(
            root=source.parent / ".toolang",
            name=source.stem,
            placement="roaming",
        )

    @property
    def home(self) -> Path:
        return self.root / "agents" / self.name

    @property
    def root_config(self) -> Path:
        return self.root / "config.toml"

    @property
    def root_env(self) -> Path:
        return self.root / ".env"

    @property
    def program(self) -> Path:
        return self.home / "agent.too"

    @property
    def config(self) -> Path:
        return self.home / "config.toml"

    @property
    def env(self) -> Path:
        return self.home / ".env"

    @property
    def collab(self) -> Path:
        return self.home / "collab"

    @property
    def collab_memo(self) -> Path:
        return self.collab / "MEMO.md"

    @property
    def lab(self) -> Path:
        return self.home / "lab"

    @property
    def lab_memo(self) -> Path:
        return self.lab / "MEMO.md"

    @property
    def root_setup(self) -> Path:
        return self.root / ".setup"

    @property
    def home_setup(self) -> Path:
        return self.home / ".setup"

    @property
    def model_cache(self) -> Path:
        return self.root_setup / "models"

    @property
    def root_state(self) -> Path:
        return self.root / ".state"

    @property
    def home_state(self) -> Path:
        return self.home / ".state"

    @property
    def root_runtime(self) -> Path:
        return self.root / ".runtime"

    @property
    def runtime(self) -> Path:
        return self.home / ".runtime"

    @property
    def runtime_status(self) -> Path:
        return self.runtime / "status.json"

    @property
    def hosting_state(self) -> Path:
        return self.runtime / "hosting.json"

    @property
    def run_store(self) -> Path:
        return self.runtime / "runs.db"

    @property
    def job_store(self) -> Path:
        return self.runtime / "jobs.db"

    @property
    def file_store(self) -> Path:
        return self.runtime / "files.db"

    @property
    def id_state(self) -> Path:
        return self.runtime / "ids.json"

    @property
    def runtime_log(self) -> Path:
        return self.runtime / "agent.log"

    @property
    def hosted_workspaces(self) -> Path:
        return self.runtime / "workspaces"

    def run_log(self, runnable: str | None, run_id: str) -> Path:
        """Return the log path for one script invocation."""

        return (
            self.runtime
            / "logs"
            / _safe_log_label(runnable or "default")
            / f"{run_id}.log"
        )

    def tool_room(self, plugin: str) -> Path:
        return self.runtime / "tools" / plugin

    def channel_room(self, binding: str) -> Path:
        return self.runtime / "channels" / binding

    @property
    def sandbox_stage(self) -> Path:
        return self.root / ".sandbox" / self.name


def _safe_agent_label(value: str) -> str:
    text = "".join(
        char.lower() if char.isalnum() or char in {"-", "_"} else "-"
        for char in value.strip()
    )
    return text.strip("-_") or "agent"


def _safe_log_label(value: str) -> str:
    text = "".join(
        char if char.isalnum() or char in {"-", "_", "."} else "_"
        for char in value.strip()
    )
    return text.strip("._") or "default"
