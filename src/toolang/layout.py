from __future__ import annotations

from pathlib import Path

CAP_KINDS = ("skills", "services", "prompts", "psyches")


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


def sandbox_host(root: Path | str, agent_name: str) -> Path:
    return resolve_toolang_root(root) / "sandbox" / agent_name


def bus_dir(root: Path | str) -> Path:
    return resolve_toolang_root(root) / "bus"


def bus_events_db_path(root: Path | str) -> Path:
    return bus_dir(root) / "events.db"


def bus_run_path(root: Path | str) -> Path:
    return bus_dir(root) / "bus.run"


def bus_log_path(root: Path | str) -> Path:
    return bus_dir(root) / "bus.log"


def toolang_config_path(agent_home: Path | str) -> Path:
    return resolve_agent_home(agent_home) / "toolang.toml"


def toolang_lock_path(agent_home: Path | str) -> Path:
    return resolve_agent_home(agent_home) / "toolang.lock"


def agent_source_path(agent_home: Path | str, agent_name: str) -> Path:
    return resolve_agent_home(agent_home) / f"{agent_name}.too"


def agent_room(agent_home: Path | str, agent_name: str) -> Path:
    return resolve_agent_home(agent_home) / ".toolang" / "agent" / agent_name


def synced_caps_root(agent_home: Path | str) -> Path:
    return resolve_agent_home(agent_home) / ".toolang" / ".sync"


def synced_caps_dir(agent_home: Path | str, kind: str) -> Path:
    _validate_cap_kind(kind)
    return synced_caps_root(agent_home) / kind


def shared_caps_root(agent_home: Path | str) -> Path:
    return resolve_agent_home(agent_home) / ".toolang"


def shared_caps_dir(agent_home: Path | str, kind: str) -> Path:
    _validate_cap_kind(kind)
    return shared_caps_root(agent_home) / kind


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
    shared_root = shared_caps_root(resolved_home)
    for path in (resolved_home, room, sync_root):
        path.mkdir(parents=True, exist_ok=True)
    for kind in CAP_KINDS:
        (sync_root / kind).mkdir(parents=True, exist_ok=True)
        (shared_root / kind).mkdir(parents=True, exist_ok=True)
    return resolved_home


def _validate_cap_kind(kind: str) -> None:
    if kind not in CAP_KINDS:
        raise ValueError(f"Unsupported capability kind: {kind}")
