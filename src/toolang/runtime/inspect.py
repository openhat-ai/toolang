"""Runtime inspect-plane helpers."""

from __future__ import annotations

from toolang.agent.prepared import PreparedAgent
from toolang.caps import load_prepared_caps
from toolang.channels import ChannelPlugin
from toolang.concepts.layout import AgentHome, AgentRoom, ToolangRoot
from toolang.concepts.persisted import (
    ChannelBinding,
    ChannelsConfig,
    HooksConfig,
    PollState,
)
from toolang.concepts.sandbox import SandboxSpec
from toolang.tools import create_tool_runtime


def runtime_diagnostics_snapshot(
    *,
    prepared: PreparedAgent,
    room: AgentRoom,
    sandbox: str,
    runtime_loops: tuple[str, ...],
    channels_config: ChannelsConfig,
    channel_plugins: dict[str, ChannelPlugin],
    scheduler_snapshot: dict[str, object],
    pulse_pending: set[str],
) -> dict[str, object]:
    """Return one operational diagnostics snapshot for the active runtime."""

    home = AgentHome.resolve(prepared.ref.home)
    hooks_config = (
        HooksConfig.load(home.hooks_config_path)
        if home.hooks_config_path.exists()
        else HooksConfig()
    )
    channels: list[dict[str, object]] = []
    for name, binding in sorted(channels_config.channels.items()):
        channels.append(
            _channel_diagnostics(
                room=room,
                name=name,
                binding=binding,
                plugin=channel_plugins.get(name),
            )
        )
    hooks = [
        {
            "name": name,
            "path": binding.path,
            "method": binding.method,
            "plugin": binding.plugin,
        }
        for name, binding in sorted(hooks_config.hooks.items())
    ]
    pulse = None
    if "pulse" in runtime_loops or room.pulse_state_path.exists() or pulse_pending:
        pulse = {
            "state_path": str(room.pulse_state_path),
            "pending": sorted(pulse_pending),
        }
    return {
        "runtime_loops": list(runtime_loops),
        "hook_loop_enabled": "hook" in runtime_loops,
        "security": runtime_security_snapshot(
            prepared=prepared,
            room=room,
            sandbox=sandbox,
            runtime_loops=runtime_loops,
        ),
        "scheduler": scheduler_snapshot,
        "channels": channels,
        "hooks": hooks,
        "pulse": pulse,
    }


def runtime_security_snapshot(
    *,
    prepared: PreparedAgent,
    room: AgentRoom,
    sandbox: str,
    runtime_loops: tuple[str, ...],
) -> dict[str, object]:
    """Return one structured security snapshot for the active runtime."""

    spec = SandboxSpec.parse(sandbox)
    visible_caps = load_prepared_caps(prepared)
    tool_runtime = create_tool_runtime(
        prepared.ref,
        sandbox=sandbox,
        working_directory=prepared.ref.home,
        visible_services=[
            item.service_catalog_item() for item in visible_caps.services
        ],
    )
    enabled_families = set(tool_runtime.enabled_families())
    pulse_enabled = "pulse" in runtime_loops
    caps_mutable = prepared.ref.kind != "visiting"
    return {
        "sandbox": _sandbox_security_snapshot(
            spec=spec,
            prepared=prepared,
            room=room,
        ),
        "tools": {
            "filesystem": "filesystem" in enabled_families,
            "shell": "shell" in enabled_families,
            "browser_use": "browser_use" in enabled_families,
            "computer_use": "computer_use" in enabled_families,
            "service_use": "service_use" in enabled_families,
            "web_search": "web_search" in enabled_families,
            "mem_search": "memory_search" in enabled_families,
            "file_search": False,
        },
        "autonomy": {
            "chores_enabled": pulse_enabled,
            "tasks_enabled": pulse_enabled,
            "will_enabled": pulse_enabled,
            "will_path_exists": room.will_path.exists(),
        },
        "self_modification": {
            "can_add_caps": caps_mutable,
            "can_edit_will": True,
            "can_write_source": False,
            "can_persist_changes": True,
        },
    }


def _channel_diagnostics(
    *,
    room: AgentRoom,
    name: str,
    binding: ChannelBinding,
    plugin: ChannelPlugin | None,
) -> dict[str, object]:
    health_ok: bool | None = None
    health_detail: str | None = None
    health_meta: dict[str, object] = {}
    if plugin is not None:
        try:
            health = plugin.health()
        except Exception as exc:
            health_ok = False
            health_detail = str(exc)
        else:
            health_ok = health.ok
            health_detail = health.detail
            health_meta = dict(health.meta)
    poll_state_path = room.poll_state_path(name)
    poll_state = PollState.load(poll_state_path) if poll_state_path.exists() else None
    return {
        "name": name,
        "plugin": binding.plugin,
        "ok": health_ok,
        "detail": health_detail,
        "meta": health_meta,
        "poll_state_path": str(poll_state_path),
        "poll_cursor": poll_state.cursor if poll_state is not None else None,
        "poll_meta": dict(poll_state.meta) if poll_state is not None else {},
    }


def _sandbox_security_snapshot(
    *,
    spec: SandboxSpec,
    prepared: PreparedAgent,
    room: AgentRoom,
) -> dict[str, object]:
    if spec.kind != "docker":
        return {
            "image": None,
            "volumes": [],
            "network_mode": "host",
            "bridge": None,
            "dns": [],
            "host_reachability": True,
        }
    root = ToolangRoot.resolve(prepared.ref.root)
    stage_dir = root.sandbox_dir(_sandbox_key(prepared.ref.name, prepared.ref.id))
    volumes = [f"{root.path}:{root.path}"]
    if not _path_is_within(prepared.ref.home, root.path):
        volumes.append(f"{prepared.ref.home}:{prepared.ref.home}")
    volumes.append(f"{stage_dir}:{room.sandbox_dir}")
    return {
        "image": spec.image,
        "volumes": volumes,
        "network_mode": "bridge",
        "bridge": "default",
        "dns": [],
        "host_reachability": False,
    }


def _sandbox_key(agent_name: str, agent_id: str) -> str:
    return f"{agent_name}-{agent_id[:12]}"


def _path_is_within(path, parent) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True
