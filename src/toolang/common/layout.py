"""Canonical filesystem layout for one materialized agent."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Literal

AgentPlacement = Literal["resident", "visiting", "roaming"]

IMPLICIT_WORKSPACE_NAME = "lab"


@dataclass(frozen=True, slots=True)
class AgentLayout:
    """Immutable paths for one agent under one materialized Toolang root."""

    root: Path
    name: str
    placement: AgentPlacement
    instance: str | None = None
    project_inputs: bool | None = None
    managed_membership: bool | None = None
    toolang_root: Path | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        root = self.root.expanduser().resolve()
        name = self.name.strip()
        if self.placement not in {"resident", "visiting", "roaming"}:
            raise ValueError(f"invalid agent placement: {self.placement!r}")
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ValueError(f"invalid agent name: {self.name!r}")
        if self.instance is not None and (
            not self.instance or not self.instance.isalnum()
        ):
            raise ValueError("instance identity must be alphanumeric")
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "name", name)
        if self.toolang_root is not None:
            object.__setattr__(
                self, "toolang_root", self.toolang_root.expanduser().resolve()
            )
        if self.project_inputs is None:
            object.__setattr__(self, "project_inputs", self.placement == "roaming")
        if self.managed_membership is None:
            object.__setattr__(self, "managed_membership", self.placement == "resident")

    @classmethod
    def resident(cls, root: Path, name: str) -> AgentLayout:
        """Create a resident-agent layout under an explicit Toolang root."""

        return cls(root=root, name=name, placement="resident", toolang_root=root)

    @classmethod
    def visiting(
        cls, source: str, name: str, *, toolang_root: Path | None = None
    ) -> AgentLayout:
        """Create the stable temporary layout for one remote source selector."""

        label = _safe_agent_label(name)
        identity = (
            source
            if toolang_root is None
            else f"{toolang_root.expanduser().resolve()}\0{source}"
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
        return cls(
            root=Path("/tmp") / f"toolang-{label}-{digest}",
            name=name,
            placement="visiting",
            toolang_root=toolang_root,
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
    def root_state(self) -> Path:
        return self.root / ".state" / "root"

    @property
    def home_state(self) -> Path:
        return self.home / ".state" / "home"

    @property
    def agent_state(self) -> Path:
        return self.home / ".state" / "agent"

    @property
    def root_edit_lock(self) -> Path:
        return root_edit_lock_path(self.root)

    @property
    def home_edit_lock(self) -> Path:
        return home_edit_lock_path(self.home)

    @property
    def root_runtime(self) -> Path:
        return self.root / ".runtime"

    @property
    def runtime(self) -> Path:
        base = self.home / ".runtime"
        return base / "instances" / self.instance if self.instance is not None else base

    @property
    def runtime_status(self) -> Path:
        return self.home / ".runtime" / "status.json"

    @property
    def sandbox_state(self) -> Path:
        """Return the consolidated runtime record (including control refs)."""
        return self.runtime_status

    @property
    def previous_sandbox_state(self) -> Path:
        return self.sandbox_home / "state.json"

    @property
    def legacy_sandbox_state(self) -> Path:
        """Return the former guest-writable sandbox control-state path."""

        return self.runtime / "sandbox.json"

    @property
    def run_store(self) -> Path:
        return self.runtime / "runs.db"

    @property
    def chore_store(self) -> Path:
        return self.run_store

    @property
    def runtime_log(self) -> Path:
        return self.runtime / "agent.log"

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

    @property
    def sandbox_home(self) -> Path:
        base = self.root / ".sandbox" / self.name
        return base / "instances" / self.instance if self.instance is not None else base

    @property
    def sandbox_stage(self) -> Path:
        return self.sandbox_home / "launches"


def root_edit_lock_path(root: Path) -> Path:
    return root / ".state" / "root" / "edit.lock"


def home_edit_lock_path(home: Path) -> Path:
    return home / ".state" / "home" / "edit.lock"


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


def ensure_scratch_workspace(home: Path) -> Path:
    """Create and validate one agent-owned implicit scratch workspace."""

    resolved_home = home.expanduser().resolve()
    if not resolved_home.is_dir():
        raise FileNotFoundError(f"agent home is not available: {resolved_home}")
    root = resolved_home / IMPLICIT_WORKSPACE_NAME
    if root.is_symlink():
        raise ValueError(f"implicit lab workspace must not be a symlink: {root}")
    root.mkdir(exist_ok=True)
    if not root.is_dir() or root.resolve().parent != resolved_home:
        raise ValueError(f"implicit lab workspace is not a directory: {root}")
    return root
