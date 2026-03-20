from __future__ import annotations

from pathlib import Path

from toolang_caps.models import CAP_KINDS, CapKind, section_name


def resolve_toolang_root(root: Path | str) -> Path:
    return Path(root).expanduser().resolve()


def resolve_agent_home(agent_home: Path | str) -> Path:
    return Path(agent_home).expanduser().resolve()


def agents_db_path(root: Path | str) -> Path:
    return resolve_toolang_root(root) / "agents.db"


def resident_agent_home(root: Path | str, home_name: str) -> Path:
    return resolve_toolang_root(root) / "agents" / home_name


def visiting_agent_home(root: Path | str, home_name: str) -> Path:
    return resolve_toolang_root(root) / "guests" / home_name


def sandbox_host(root: Path | str, sandbox_name: str) -> Path:
    return resolve_toolang_root(root) / "sandbox" / sandbox_name


def sandbox_args_path(root: Path | str, sandbox_name: str) -> Path:
    return sandbox_host(root, sandbox_name) / "args.json"


def sandbox_exec_path(root: Path | str, sandbox_name: str) -> Path:
    return sandbox_host(root, sandbox_name) / "exec.sh"


def bus_dir(root: Path | str) -> Path:
    return resolve_toolang_root(root) / "bus"


def bus_events_db_path(root: Path | str) -> Path:
    return bus_dir(root) / "events.db"


def bus_run_path(root: Path | str) -> Path:
    return bus_dir(root) / "bus.run"


def bus_log_path(root: Path | str) -> Path:
    return bus_dir(root) / "bus.log"


def agent_source_path(agent_home: Path | str, agent_name: str) -> Path:
    return resolve_agent_home(agent_home) / f"{agent_name}.too"


def shared_source_path(agent_home: Path | str) -> Path:
    return resolve_agent_home(agent_home) / "agents.too"


def global_source_path(root: Path | str) -> Path:
    return resolve_toolang_root(root) / "agents.too"


def agent_room(agent_home: Path | str, agent_name: str) -> Path:
    return resolve_agent_home(agent_home) / ".toolang" / "agents" / agent_name


def agent_run_path(agent_home: Path | str, agent_name: str) -> Path:
    return agent_room(agent_home, agent_name) / "agent.run"


def agent_log_path(agent_home: Path | str, agent_name: str) -> Path:
    return agent_room(agent_home, agent_name) / "agent.log"


def agent_chats_dir(agent_home: Path | str, agent_name: str) -> Path:
    return agent_room(agent_home, agent_name) / "chats"


def agent_chats_db_path(agent_home: Path | str, agent_name: str) -> Path:
    return agent_chats_dir(agent_home, agent_name) / "chats.db"


def agent_room_sandbox_dir(agent_home: Path | str, agent_name: str) -> Path:
    return agent_room(agent_home, agent_name) / "sandbox"


def agent_sync_path(agent_home: Path | str, agent_name: str) -> Path:
    return synced_caps_root(agent_home) / f"{agent_name}.state.json"


def synced_caps_root(agent_home: Path | str) -> Path:
    return resolve_agent_home(agent_home) / ".toolang" / "sync"


def synced_caps_dir(agent_home: Path | str, kind: CapKind) -> Path:
    return synced_caps_root(agent_home) / section_name(kind)


def agent_synced_caps_root(agent_home: Path | str, agent_name: str) -> Path:
    return agent_room(agent_home, agent_name) / "sync"


def agent_synced_caps_dir(agent_home: Path | str, agent_name: str, kind: CapKind) -> Path:
    return agent_synced_caps_root(agent_home, agent_name) / section_name(kind)


def global_synced_caps_root(root: Path | str) -> Path:
    return resolve_toolang_root(root) / "sync"


def global_synced_caps_dir(root: Path | str, kind: CapKind) -> Path:
    return global_synced_caps_root(root) / section_name(kind)


def shared_caps_root(agent_home: Path | str) -> Path:
    return resolve_agent_home(agent_home) / ".toolang"


def shared_caps_dir(agent_home: Path | str, kind: CapKind) -> Path:
    return shared_caps_root(agent_home) / section_name(kind)


def global_caps_root(root: Path | str) -> Path:
    return resolve_toolang_root(root)


def global_caps_dir(root: Path | str, kind: CapKind) -> Path:
    return global_caps_root(root) / section_name(kind)


def ensure_toolang_root_layout(root: Path | str) -> Path:
    resolved_root = resolve_toolang_root(root)
    for path in (
        resolved_root,
        resolved_root / "agents",
        resolved_root / "guests",
        resolved_root / "sandbox",
        resolved_root / "bus",
    ):
        path.mkdir(parents=True, exist_ok=True)
    return resolved_root


def ensure_agent_home_layout(agent_home: Path | str, agent_name: str) -> Path:
    resolved_home = resolve_agent_home(agent_home)
    room = agent_room(resolved_home, agent_name)
    sync_root = synced_caps_root(resolved_home)
    for path in (resolved_home, room, sync_root):
        path.mkdir(parents=True, exist_ok=True)
    for kind in CAP_KINDS:
        (sync_root / section_name(kind)).mkdir(parents=True, exist_ok=True)
    return resolved_home
