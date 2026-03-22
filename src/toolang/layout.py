"""Canonical layout facade built on stable layout concepts."""

from __future__ import annotations

from toolang.concepts.caps import CapKind
from toolang.concepts.layout import AgentHome, AgentRoom, ToolangRoot

__all__ = [
    "AgentHome",
    "AgentRoom",
    "ToolangRoot",
    "agent_chats_db_path",
    "agent_chats_dir",
    "agent_log_path",
    "agent_room",
    "agent_room_sandbox_dir",
    "agent_run_dir",
    "agent_run_path",
    "agent_run_prompt_path",
    "agent_runs_dir",
    "agent_source_path",
    "agent_sync_path",
    "agent_synced_caps_dir",
    "agent_synced_caps_root",
    "agents_db_path",
    "bus_dir",
    "bus_events_db_path",
    "bus_log_path",
    "bus_run_path",
    "cap_section_dir_name",
    "ensure_agent_home_layout",
    "ensure_toolang_root_layout",
    "global_caps_dir",
    "global_caps_root",
    "global_source_path",
    "global_synced_caps_dir",
    "global_synced_caps_root",
    "resident_agent_home",
    "resolve_agent_home",
    "resolve_toolang_root",
    "sandbox_args_path",
    "sandbox_exec_path",
    "sandbox_host",
    "shared_caps_dir",
    "shared_caps_root",
    "shared_source_path",
    "synced_caps_dir",
    "synced_caps_root",
    "visiting_agent_home",
]


_SECTION_DIR_BY_CAP_KIND = {
    "skill": "skills",
    "service": "services",
    "prompt": "prompts",
    "psyche": "psyches",
}


def resolve_toolang_root(root):
    """Return one normalized absolute Toolang root path."""

    return ToolangRoot.resolve(root).path


def resolve_agent_home(agent_home):
    """Return one normalized absolute agent home path."""

    return AgentHome.resolve(agent_home).path


def agents_db_path(root):
    """Return the known-agent registry database path for one Toolang root."""

    return ToolangRoot.resolve(root).agents_db_path


def resident_agent_home(root, home_name: str):
    """Return the resident agent home path for one resident home name."""

    return ToolangRoot.resolve(root).resident_home(home_name).path


def visiting_agent_home(root, home_name: str):
    """Return the visiting agent home path for one visiting home name."""

    return ToolangRoot.resolve(root).visiting_home(home_name).path


def sandbox_host(root, sandbox_name: str):
    """Return the sandbox staging directory for one sandbox key."""

    return ToolangRoot.resolve(root).sandbox_dir(sandbox_name)


def sandbox_args_path(root, sandbox_name: str):
    """Return the staged sandbox args file path for one sandbox key."""

    return ToolangRoot.resolve(root).sandbox_args_path(sandbox_name)


def sandbox_exec_path(root, sandbox_name: str):
    """Return the staged sandbox launcher script path for one sandbox key."""

    return ToolangRoot.resolve(root).sandbox_exec_path(sandbox_name)


def bus_dir(root):
    """Return the shared bus directory for one Toolang root."""

    return ToolangRoot.resolve(root).bus_dir


def bus_events_db_path(root):
    """Return the shared bus projection database path."""

    return ToolangRoot.resolve(root).bus_events_db_path


def bus_run_path(root):
    """Return the bus process state file path."""

    return ToolangRoot.resolve(root).bus_run_path


def bus_log_path(root):
    """Return the bus process log path."""

    return ToolangRoot.resolve(root).bus_log_path


def agent_source_path(agent_home, agent_name: str):
    """Return the authored `.too` source path for one agent."""

    return AgentHome.resolve(agent_home).source(agent_name)


def shared_source_path(agent_home):
    """Return the shared `agents.too` path for one agent home."""

    return AgentHome.resolve(agent_home).shared_source_path


def global_source_path(root):
    """Return the global `agents.too` path for one Toolang root."""

    return ToolangRoot.resolve(root).global_source_path


def agent_room(agent_home, agent_name: str):
    """Return the private machine-managed room path for one agent."""

    return AgentHome.resolve(agent_home).room(agent_name).path


def agent_run_path(agent_home, agent_name: str):
    """Return the current running-state file path for one agent."""

    return AgentHome.resolve(agent_home).room(agent_name).run_path


def agent_log_path(agent_home, agent_name: str):
    """Return the runtime log file path for one agent."""

    return AgentHome.resolve(agent_home).room(agent_name).log_path


def agent_chats_dir(agent_home, agent_name: str):
    """Return the chat-state directory for one agent."""

    return AgentHome.resolve(agent_home).room(agent_name).chats_dir


def agent_chats_db_path(agent_home, agent_name: str):
    """Return the chat transcript database path for one agent."""

    return AgentHome.resolve(agent_home).room(agent_name).chats_db_path


def agent_runs_dir(agent_home, agent_name: str):
    """Return the per-run trace directory root for one agent."""

    return AgentHome.resolve(agent_home).room(agent_name).runs_dir


def agent_run_dir(agent_home, agent_name: str, run_id: str):
    """Return the trace directory for one run."""

    return AgentHome.resolve(agent_home).room(agent_name).run_dir(run_id)


def agent_run_prompt_path(agent_home, agent_name: str, run_id: str):
    """Return the prompt trace path for one run."""

    return AgentHome.resolve(agent_home).room(agent_name).prompt_trace_path(run_id)


def agent_room_sandbox_dir(agent_home, agent_name: str):
    """Return the sandbox staging directory inside one agent room."""

    return AgentHome.resolve(agent_home).room(agent_name).sandbox_dir


def agent_sync_path(agent_home, agent_name: str):
    """Return the synced state file path for one agent."""

    return AgentHome.resolve(agent_home).sync_state_path(agent_name)


def synced_caps_root(agent_home):
    """Return the shared synced-cap root for one agent home."""

    return AgentHome.resolve(agent_home).synced_caps_root


def synced_caps_dir(agent_home, kind: CapKind):
    """Return the shared synced-cap directory for one cap kind."""

    return AgentHome.resolve(agent_home).synced_caps_dir(kind)


def agent_synced_caps_root(agent_home, agent_name: str):
    """Return the agent-only synced-cap root for one agent."""

    return AgentHome.resolve(agent_home).room(agent_name).synced_caps_root


def agent_synced_caps_dir(agent_home, agent_name: str, kind: CapKind):
    """Return the agent-only synced-cap directory for one cap kind."""

    return AgentHome.resolve(agent_home).room(agent_name).synced_caps_dir(kind)


def global_synced_caps_root(root):
    """Return the global synced-cap root for one Toolang root."""

    return ToolangRoot.resolve(root).global_synced_caps_root


def global_synced_caps_dir(root, kind: CapKind):
    """Return the global synced-cap directory for one cap kind."""

    return ToolangRoot.resolve(root).global_synced_caps_dir(kind)


def shared_caps_root(agent_home):
    """Return the shared local-cap root for one agent home."""

    return AgentHome.resolve(agent_home).shared_caps_root


def shared_caps_dir(agent_home, kind: CapKind):
    """Return the shared local-cap directory for one cap kind."""

    return AgentHome.resolve(agent_home).shared_caps_dir(kind)


def global_caps_root(root):
    """Return the global local-cap root for one Toolang root."""

    return ToolangRoot.resolve(root).global_caps_root


def global_caps_dir(root, kind: CapKind):
    """Return the global local-cap directory for one cap kind."""

    return ToolangRoot.resolve(root).global_caps_dir(kind)


def cap_section_dir_name(kind: CapKind) -> str:
    """Return the directory section name used for one cap kind."""

    return _SECTION_DIR_BY_CAP_KIND[kind]


def ensure_toolang_root_layout(root):
    """Create the required top-level Toolang root directories if missing."""

    return ToolangRoot.resolve(root).ensure_layout().path


def ensure_agent_home_layout(agent_home, agent_name: str):
    """Create the machine-managed directories required for one agent home."""

    return AgentHome.resolve(agent_home).ensure_layout(agent_name=agent_name).path
