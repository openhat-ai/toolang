"""Canonical Toolang layout concepts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast, get_args

from .caps import CapKind

_SECTION_DIR_BY_CAP_KIND = {
    "skill": "skills",
    "service": "services",
    "prompt": "prompts",
    "psyche": "psyches",
}
_ALL_CAP_KINDS = tuple(cast(CapKind, kind) for kind in get_args(CapKind))


def _section_dir_name(kind: CapKind) -> str:
    return _SECTION_DIR_BY_CAP_KIND[kind]


@dataclass(frozen=True, slots=True)
class ToolangRoot:
    """One normalized Toolang root with path-derived layout helpers."""

    path: Path

    @classmethod
    def resolve(cls, root: Path | str) -> "ToolangRoot":
        return cls(Path(root).expanduser().resolve())

    @property
    def agents_db_path(self) -> Path:
        return self.path / "agents.db"

    @property
    def config_path(self) -> Path:
        return self.path / "config.toml"

    @property
    def bus_dir(self) -> Path:
        return self.path / "bus"

    @property
    def bus_events_db_path(self) -> Path:
        return self.bus_dir / "events.db"

    @property
    def bus_run_path(self) -> Path:
        return self.bus_dir / "bus.run"

    @property
    def bus_log_path(self) -> Path:
        return self.bus_dir / "bus.log"

    @property
    def global_source_path(self) -> Path:
        return self.path / "agents.too"

    @property
    def global_synced_caps_root(self) -> Path:
        return self.path / "sync"

    @property
    def global_caps_root(self) -> Path:
        return self.path

    def resident_home(self, home_name: str) -> "AgentHome":
        return AgentHome.resolve(self.path / "agents" / home_name)

    def visiting_home(self, home_name: str) -> "AgentHome":
        return AgentHome.resolve(self.path / "guests" / home_name)

    def sandbox_dir(self, sandbox_name: str) -> Path:
        return self.path / "sandbox" / sandbox_name

    def sandbox_args_path(self, sandbox_name: str) -> Path:
        return self.sandbox_dir(sandbox_name) / "args.json"

    def sandbox_exec_path(self, sandbox_name: str) -> Path:
        return self.sandbox_dir(sandbox_name) / "exec.sh"

    def global_synced_caps_dir(self, kind: CapKind) -> Path:
        return self.global_synced_caps_root / _section_dir_name(kind)

    def global_caps_dir(self, kind: CapKind) -> Path:
        return self.global_caps_root / _section_dir_name(kind)

    def ensure_layout(self) -> "ToolangRoot":
        for path in (
            self.path,
            self.path / "agents",
            self.path / "guests",
            self.path / "sandbox",
            self.bus_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self


@dataclass(frozen=True, slots=True)
class AgentHome:
    """One normalized agent home with home-relative layout helpers."""

    path: Path

    @classmethod
    def resolve(cls, agent_home: Path | str) -> "AgentHome":
        return cls(Path(agent_home).expanduser().resolve())

    @property
    def shared_source_path(self) -> Path:
        return self.path / "agents.too"

    @property
    def channels_config_path(self) -> Path:
        return self.path / "channels.toml"

    @property
    def hooks_config_path(self) -> Path:
        return self.path / "hooks.toml"

    @property
    def tools_config_path(self) -> Path:
        return self.path / "tools.toml"

    @property
    def synced_caps_root(self) -> Path:
        return self.path / ".toolang" / "sync"

    @property
    def shared_caps_root(self) -> Path:
        return self.path / ".toolang"

    def source(self, agent_name: str) -> Path:
        return self.path / f"{agent_name}.too"

    def room(self, agent_name: str) -> "AgentRoom":
        return AgentRoom(self.shared_caps_root / "agents" / agent_name)

    def sync_state_path(self, agent_name: str) -> Path:
        return self.synced_caps_root / f"{agent_name}.state.json"

    def synced_caps_dir(self, kind: CapKind) -> Path:
        return self.synced_caps_root / _section_dir_name(kind)

    def shared_caps_dir(self, kind: CapKind) -> Path:
        return self.shared_caps_root / _section_dir_name(kind)

    def ensure_layout(self, *, agent_name: str) -> "AgentHome":
        room = self.room(agent_name)
        for path in (self.path, room.path, self.synced_caps_root):
            path.mkdir(parents=True, exist_ok=True)
        for kind in _ALL_CAP_KINDS:
            self.synced_caps_dir(kind).mkdir(parents=True, exist_ok=True)
        return self


@dataclass(frozen=True, slots=True)
class AgentRoom:
    """One normalized private agent room with room-relative layout helpers."""

    path: Path

    @property
    def run_path(self) -> Path:
        return self.path / "agent.run"

    @property
    def origin_path(self) -> Path:
        return self.path / "agent.origin.json"

    @property
    def log_path(self) -> Path:
        return self.path / "agent.log"

    @property
    def chats_dir(self) -> Path:
        return self.path / "chats"

    @property
    def chats_db_path(self) -> Path:
        return self.chats_dir / "chats.db"

    @property
    def execution_db_path(self) -> Path:
        return self.path / "execution.db"

    @property
    def runs_dir(self) -> Path:
        return self.path / "runs"

    @property
    def sandbox_dir(self) -> Path:
        return self.path / "sandbox"

    @property
    def poll_dir(self) -> Path:
        return self.path / "poll"

    @property
    def hooks_dir(self) -> Path:
        return self.path / "hooks"

    @property
    def tasks_dir(self) -> Path:
        return self.path / "tasks"

    @property
    def task_mirrors_path(self) -> Path:
        return self.path / "task_mirrors.json"

    @property
    def chores_dir(self) -> Path:
        return self.path / "chores"

    @property
    def will_path(self) -> Path:
        return self.path / "will.md"

    @property
    def pulse_state_path(self) -> Path:
        return self.path / "pulse.json"

    def poll_state_path(self, binding_name: str) -> Path:
        return self.poll_dir / f"{binding_name}.json"

    @property
    def synced_caps_root(self) -> Path:
        return self.path / "sync"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def prompt_trace_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "prompt.json"

    def synced_caps_dir(self, kind: CapKind) -> Path:
        return self.synced_caps_root / _section_dir_name(kind)
