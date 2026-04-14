"""Minimal managed-agent file operations."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import socket
import shutil
import time
from urllib.parse import urlsplit
from collections.abc import Sequence

from toolang.base.protocols.sandbox import SandboxPlugin
from toolang.base.types.sandbox import SandboxState
from .sandboxes.docker import docker_container_identity, docker_container_running
from . import templates


@dataclass(frozen=True, slots=True)
class AgentStatus:
    """One listed agent status row."""

    name: str
    status: str
    endpoint: str | None
    api_url: str | None
    webui_url: str | None
    sandbox: str | None


def agent_home(toolang_root: Path, agent_name: str) -> Path:
    """Return one agent home path."""

    return toolang_root / "agents" / agent_name


def agent_program_path(toolang_root: Path, agent_name: str) -> Path:
    """Return one agent program path."""

    return agent_home(toolang_root, agent_name) / f"{agent_name}.too"


def agent_room(toolang_root: Path, agent_name: str) -> Path:
    """Return one agent room path."""

    return agent_home(toolang_root, agent_name) / ".runtime"


def agent_runtime_state_path(toolang_root: Path, agent_name: str) -> Path:
    """Return one agent runtime state path."""

    return agent_room(toolang_root, agent_name) / "runtime.json"


def agent_runtime_log_path(toolang_root: Path, agent_name: str) -> Path:
    """Return one agent process log path."""

    return agent_room(toolang_root, agent_name) / "agent.log"


def agent_pulse_state_path(toolang_root: Path, agent_name: str) -> Path:
    """Return one agent pulse-state path."""

    return agent_room(toolang_root, agent_name) / "pulse.json"


def tool_room(toolang_root: Path, agent_name: str, plugin_name: str) -> Path:
    """Return one tool-plugin room path."""

    return agent_room(toolang_root, agent_name) / "tools" / plugin_name


def channel_room(toolang_root: Path, agent_name: str, binding_name: str) -> Path:
    """Return one channel-plugin room path."""

    return agent_room(toolang_root, agent_name) / "channels" / binding_name


def _sandbox_stage_dir(toolang_root: Path, agent_name: str) -> Path:
    return toolang_root / ".sandbox" / agent_name


def _default_program_source(agent_name: str, *, template_name: str) -> str:
    return templates.render_template("agent", template_name, agent_name=agent_name, name=agent_name)


def _rewrite_program_source(source_text: str, agent_name: str) -> str:
    lines = source_text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip().startswith("agent "):
            suffix = "\n" if line.endswith("\n") else ""
            lines[index] = f"agent {agent_name}{suffix}"
            return "".join(lines)
    return source_text


def create_agent(toolang_root: Path, agent_name: str, *, template_name: str = "default") -> Path:
    """Create one new agent."""

    home = agent_home(toolang_root, agent_name)
    if home.exists():
        raise FileExistsError(f"agent already exists: {home}")
    home.mkdir(parents=True, exist_ok=False)
    program_path = agent_program_path(toolang_root, agent_name)
    program_path.write_text(
        _default_program_source(agent_name, template_name=template_name),
        encoding="utf-8",
    )
    return program_path


def clone_agent(toolang_root: Path, source_name: str, target_name: str) -> Path:
    """Clone one agent into a new name."""

    source_home = agent_home(toolang_root, source_name)
    target_home = agent_home(toolang_root, target_name)
    if not source_home.is_dir():
        raise FileNotFoundError(f"source agent not found: {source_home}")
    if target_home.exists():
        raise FileExistsError(f"target agent already exists: {target_home}")

    shutil.copytree(source_home, target_home, ignore=shutil.ignore_patterns(".prepared"))

    copied_source_program = target_home / f"{source_name}.too"
    target_program = target_home / f"{target_name}.too"
    if copied_source_program.is_file():
        source_text = copied_source_program.read_text(encoding="utf-8")
        copied_source_program.unlink()
    else:
        source_text = _default_program_source(target_name, template_name="default")
    target_program.write_text(_rewrite_program_source(source_text, target_name), encoding="utf-8")
    return target_program


def remove_agent(toolang_root: Path, agent_name: str) -> Path:
    """Remove one stopped agent and its local sandbox staging."""

    home = agent_home(toolang_root, agent_name)
    if not home.is_dir():
        raise FileNotFoundError(f"agent not found: {home}")
    status = get_agent_status(toolang_root, agent_name, ui_base_url="")
    if status is not None and status.status in {"running", "preparing", "starting"}:
        raise ValueError(f"agent is still active: {agent_name}")
    shutil.rmtree(home)
    sandbox_stage_dir = _sandbox_stage_dir(toolang_root, agent_name)
    if sandbox_stage_dir.exists():
        shutil.rmtree(sandbox_stage_dir)
    return home


def load_runtime_state(toolang_root: Path, agent_name: str) -> dict[str, object] | None:
    """Load one persisted runtime state when present."""

    return _load_runtime_state(agent_runtime_state_path(toolang_root, agent_name))


def write_runtime_state(
    toolang_root: Path,
    agent_name: str,
    *,
    endpoint: str,
    started_at: str,
    pid: int | None,
    sandbox: dict[str, object] | None = None,
    loops: Sequence[str] | None = None,
    status: str = "running",
    message: str | None = None,
) -> Path:
    """Persist one minimal runtime state file for a running agent."""

    path = agent_runtime_state_path(toolang_root, agent_name)
    _save_runtime_state(
        path,
        {
            "agent": agent_name,
            "status": status,
            "endpoint": endpoint,
            "started_at": started_at,
            "updated_at": started_at,
            "pid": pid,
            "sandbox": sandbox,
            "loops": list(loops or ()),
            "message": message,
        },
    )
    return path


def stop_runtime_state(toolang_root: Path, agent_name: str) -> None:
    """Mark one runtime state as stopped while keeping the last endpoint."""

    path = agent_runtime_state_path(toolang_root, agent_name)
    runtime_state = _load_runtime_state(path)
    if runtime_state is None:
        return
    runtime_state["status"] = "stopped"
    runtime_state["pid"] = None
    runtime_state["message"] = None
    runtime_state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save_runtime_state(path, runtime_state)


def stop_agent(
    toolang_root: Path,
    agent_name: str,
    *,
    sandbox_plugin: SandboxPlugin | None = None,
    force: bool = False,
) -> bool:
    """Stop one running agent and mark its runtime state as stopped."""

    runtime_state = load_runtime_state(toolang_root, agent_name)
    if runtime_state is None:
        raise FileNotFoundError(f"runtime state not found: {agent_runtime_state_path(toolang_root, agent_name)}")

    pid = runtime_state.get("pid")
    sandbox = runtime_state.get("sandbox")
    stopped = False
    if isinstance(sandbox, dict):
        if sandbox_plugin is None:
            raise ValueError("sandbox plugin is required to stop a sandboxed agent")
        sandbox_state = SandboxState.from_data(sandbox)
        if sandbox_state.runtime_id:
            sandbox_plugin.stop(sandbox_state, force=force)
            stopped = True
    if isinstance(pid, int) and _pid_alive(pid):
        _stop_pid(pid, force=force)
        stopped = True
    if stopped:
        _wait_for_endpoint_release(runtime_state.get("endpoint"), timeout_sec=5.0)

    stop_runtime_state(toolang_root, agent_name)
    return stopped


def preferred_runtime_port(toolang_root: Path, agent_name: str) -> int | None:
    """Return one previously used port for an agent when available."""

    runtime_state = _load_runtime_state(agent_runtime_state_path(toolang_root, agent_name))
    raw_endpoint = runtime_state.get("endpoint") if runtime_state else None
    if not isinstance(raw_endpoint, str) or not raw_endpoint.strip():
        return None
    try:
        return urlsplit(raw_endpoint).port
    except ValueError:
        return None


def update_runtime_state(
    toolang_root: Path,
    agent_name: str,
    *,
    status: str | None = None,
    message: str | None = None,
) -> Path | None:
    """Update one existing runtime state with lightweight status fields."""

    path = agent_runtime_state_path(toolang_root, agent_name)
    runtime_state = _load_runtime_state(path)
    if runtime_state is None:
        return None
    if status is not None:
        runtime_state["status"] = status
    runtime_state["message"] = message
    runtime_state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save_runtime_state(path, runtime_state)
    return path


def list_agent_statuses(toolang_root: Path, *, ui_base_url: str) -> tuple[AgentStatus, ...]:
    """List all managed agents with runtime status and WebUI URL."""

    agents_dir = toolang_root / "agents"
    if not agents_dir.is_dir():
        return ()

    items: list[AgentStatus] = []
    for home in sorted(item for item in agents_dir.iterdir() if item.is_dir()):
        name = home.name
        status = get_agent_status(toolang_root, name, ui_base_url=ui_base_url)
        if status is not None:
            items.append(status)
    return tuple(items)


def get_agent_status(toolang_root: Path, agent_name: str, *, ui_base_url: str) -> AgentStatus | None:
    """Return one agent status row when the agent exists."""

    home = agent_home(toolang_root, agent_name)
    if not home.is_dir():
        return None
    runtime_state = _load_runtime_state(agent_runtime_state_path(toolang_root, agent_name))
    raw_endpoint = runtime_state.get("endpoint") if runtime_state else None
    raw_status = runtime_state.get("status") if runtime_state else None
    pid = runtime_state.get("pid") if runtime_state else None
    sandbox = runtime_state.get("sandbox") if runtime_state else None
    endpoint = raw_endpoint if isinstance(raw_endpoint, str) and raw_endpoint.strip() else None
    pid_alive = isinstance(pid, int) and _pid_alive(pid)
    sandbox_alive = _sandbox_alive(sandbox)
    status = _runtime_status_label(raw_status, pid_alive=pid_alive, sandbox_alive=sandbox_alive)
    return AgentStatus(
        name=agent_name,
        status=status,
        endpoint=endpoint if status in {"running", "preparing", "starting"} else None,
        api_url=_api_docs_url(endpoint) if status in {"running", "preparing", "starting"} else None,
        webui_url=_webui_url(endpoint, ui_base_url=ui_base_url) if status == "running" else None,
        sandbox=_runtime_sandbox_label(runtime_state),
    )


def runtime_pid_label(runtime_state: dict[str, object] | None) -> str | None:
    """Return one human-readable process label for runtime info output."""

    if runtime_state is None:
        return None
    sandbox = runtime_state.get("sandbox")
    if isinstance(sandbox, dict):
        sandbox_data = {str(key): value for key, value in sandbox.items()}
        runtime_id = sandbox_data.get("runtime_id")
        selector = sandbox_data.get("selector")
        if isinstance(selector, dict):
            selector_data = {str(key): value for key, value in selector.items()}
            driver = selector_data.get("driver")
            if driver == "docker" and isinstance(runtime_id, str) and runtime_id.strip():
                identity = docker_container_identity(runtime_id)
                if identity is not None:
                    container_id, pid = identity
                    return f"{container_id[:12]}:{pid}"
    pid = runtime_state.get("pid")
    if isinstance(pid, int) and pid > 0:
        return str(pid)
    return None


def _load_runtime_state(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    normalized: dict[str, object] = {}
    for key, value in payload.items():
        normalized[str(key)] = value
    return normalized


def _save_runtime_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _runtime_status_label(raw_status: object, *, pid_alive: bool, sandbox_alive: bool) -> str:
    if sandbox_alive:
        return "running"
    if isinstance(raw_status, str) and raw_status in {"preparing", "starting"}:
        if pid_alive:
            return raw_status
        return "failed"
    if pid_alive:
        return "running"
    if isinstance(raw_status, str) and raw_status == "failed":
        return "failed"
    return "stopped"


def _runtime_sandbox_label(runtime_state: dict[str, object] | None) -> str | None:
    if runtime_state is None:
        return None
    sandbox = runtime_state.get("sandbox")
    if isinstance(sandbox, dict):
        sandbox_data = {str(key): value for key, value in sandbox.items()}
        selector = sandbox_data.get("selector")
        if isinstance(selector, dict):
            selector_data = {str(key): value for key, value in selector.items()}
            driver = selector_data.get("driver")
            target = selector_data.get("target")
            if isinstance(driver, str) and driver.strip():
                if isinstance(target, str) and target.strip():
                    return f"{driver.strip()}:{target.strip()}"
                return driver.strip()
    return "none"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _stop_pid(pid: int, *, force: bool) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise ValueError(f"permission denied while stopping pid {pid}") from exc

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.05)
    if not _pid_alive(pid):
        return
    if not force:
        return
    kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
    try:
        os.kill(pid, kill_signal)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise ValueError(f"permission denied while force-stopping pid {pid}") from exc


def _wait_for_endpoint_release(endpoint: object, *, timeout_sec: float) -> bool:
    target = _endpoint_host_port(endpoint)
    if target is None:
        return True
    host, port = target
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if _port_is_available(host, port):
            return True
        time.sleep(0.05)
    return _port_is_available(host, port)


def _endpoint_host_port(endpoint: object) -> tuple[str, int] | None:
    if not isinstance(endpoint, str) or not endpoint.strip():
        return None
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        return None
    if not parsed.hostname or parsed.port is None:
        return None
    return parsed.hostname, parsed.port


def _port_is_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _sandbox_alive(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    data = {str(key): value for key, value in payload.items()}
    runtime_id = data.get("runtime_id")
    selector = data.get("selector")
    if not isinstance(selector, dict):
        return False
    selector_data = {str(key): value for key, value in selector.items()}
    driver = selector_data.get("driver")
    if driver == "docker" and isinstance(runtime_id, str) and runtime_id.strip():
        return docker_container_running(runtime_id)
    return False


def _webui_url(endpoint: str | None, *, ui_base_url: str) -> str | None:
    if endpoint is None or not endpoint.strip():
        return None
    try:
        port = urlsplit(endpoint).port
    except ValueError:
        return None
    if port is None:
        return None
    return f"{ui_base_url.rstrip('/')}/{port}"


def _api_docs_url(endpoint: str | None) -> str | None:
    if endpoint is None or not endpoint.strip():
        return None
    return f"{endpoint.rstrip('/')}/docs"
