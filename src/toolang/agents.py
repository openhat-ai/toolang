"""Managed-agent file operations and agent source resolution."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import shlex
import signal
import shutil
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from collections.abc import Sequence
from typing import Iterator, Literal

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


AgentSelectorForm = Literal["name", "shorthand", "ref"]


@dataclass(frozen=True, slots=True)
class HttpAgentRef:
    """One canonical HTTP(S) agent program ref."""

    url: str

    def render(self) -> str:
        return self.url

    def default_name(self) -> str:
        path = urlsplit(self.url).path.rstrip("/")
        if not path:
            raise ValueError(f"invalid agent ref: {self.url}")
        return Path(path).stem


@dataclass(frozen=True, slots=True)
class GitHubAgentRef:
    """One canonical GitHub agent program ref."""

    owner: str
    repo: str
    path: str
    rev: str

    def render(self) -> str:
        return f"github://{self.owner}/{self.repo}/{self.path}@{self.rev}"

    def default_name(self) -> str:
        return Path(self.path).stem


AgentRef = HttpAgentRef | GitHubAgentRef


@dataclass(frozen=True, slots=True)
class AgentSelector:
    """One parsed agent selector."""

    form: AgentSelectorForm
    text: str
    name: str | None = None
    ref: AgentRef | None = None
    github_owner: str | None = None
    github_repo: str | None = None

    def resolved_ref(self) -> AgentRef:
        if self.ref is None:
            raise ValueError(f"selector is not a remote ref: {self.text}")
        return self.ref

    def default_name(self) -> str:
        if self.name is not None:
            return self.name
        return self.resolved_ref().default_name()


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


def agent_id_state_path(toolang_root: Path, agent_name: str) -> Path:
    """Return one agent local-id allocator state path."""

    return agent_room(toolang_root, agent_name) / "ids.json"


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


def parse_agent_selector(text: str) -> AgentSelector:
    """Parse one agent selector into a local name or one canonical remote ref."""

    raw = text.strip()
    if not raw:
        raise ValueError("agent selector cannot be empty")
    if "://" in raw:
        return AgentSelector(form="ref", text=raw, ref=_parse_agent_ref(raw))
    slash_count = raw.count("/")
    if slash_count == 1:
        left, right = raw.split("/", 1)
        if not left or not right:
            raise ValueError(f"invalid agent shorthand: {text}")
        if "." in left:
            return AgentSelector(
                form="shorthand",
                text=raw,
                ref=HttpAgentRef(url=f"https://{left}/{right}.too"),
            )
        return AgentSelector(
            form="shorthand",
            text=raw,
            name=right,
            github_owner=left,
        )
    if slash_count == 2:
        owner, repo, name = raw.split("/", 2)
        if not owner or not repo or not name:
            raise ValueError(f"invalid agent shorthand: {text}")
        return AgentSelector(
            form="shorthand",
            text=raw,
            name=name,
            github_owner=owner,
            github_repo=repo,
        )
    if slash_count > 2:
        raise ValueError(f"invalid agent shorthand: {text}")
    return AgentSelector(form="name", text=raw, name=raw)


def fetch_agent_ref(ref: AgentRef) -> str:
    """Fetch one remote agent program by canonical ref."""

    if isinstance(ref, HttpAgentRef):
        return _fetch_http_text(ref.url)
    return _fetch_github_text(ref)


def clone_agent(toolang_root: Path, source_selector: str, target_name: str | None = None) -> Path:
    """Clone one local or remote agent source into one local managed agent."""

    selector = parse_agent_selector(source_selector)
    if selector.form == "name":
        if target_name is None:
            raise ValueError("target name is required when cloning one local agent")
        return _clone_local_agent(toolang_root, selector.name or "", target_name)
    resolved_ref = resolve_agent_selector_ref(selector)
    resolved_target = target_name or selector.default_name()
    source_text = fetch_agent_ref(resolved_ref)
    return write_agent_program(toolang_root, resolved_target, source_text)


def resolve_agent_selector_ref(selector: AgentSelector) -> AgentRef:
    """Resolve one remote selector to one canonical ref."""

    if selector.ref is not None:
        return selector.ref
    if selector.github_owner is not None and selector.name is not None:
        return _resolve_github_agent_shorthand(
            selector.github_owner,
            selector.name,
            repo=selector.github_repo or "agents",
        )
    raise ValueError(f"selector is not a remote ref: {selector.text}")


def write_agent_program(toolang_root: Path, agent_name: str, source_text: str) -> Path:
    """Create one local managed agent from one source program."""

    home = agent_home(toolang_root, agent_name)
    if home.exists():
        raise FileExistsError(f"agent already exists: {home}")
    home.mkdir(parents=True, exist_ok=False)
    program_path = agent_program_path(toolang_root, agent_name)
    program_path.write_text(_rewrite_program_source(source_text, agent_name), encoding="utf-8")
    return program_path


def roaming_root(source_path: Path) -> Path:
    """Return the fixed local root for one roaming source program."""

    return source_path.resolve().parent / ".toolang"


def materialize_roaming_program(source_path: Path) -> tuple[Path, str]:
    """Materialize one local .too source into its fixed roaming root."""

    resolved_source = source_path.expanduser().resolve()
    if not resolved_source.is_file():
        raise FileNotFoundError(f"agent program not found: {resolved_source}")
    if resolved_source.suffix != ".too":
        raise ValueError(f"agent program must point to a .too file: {resolved_source}")
    toolang_root = roaming_root(resolved_source)
    agent_name = resolved_source.stem
    home = agent_home(toolang_root, agent_name)
    home.mkdir(parents=True, exist_ok=True)
    program_path = agent_program_path(toolang_root, agent_name)
    source_text = resolved_source.read_text(encoding="utf-8")
    program_path.write_text(_rewrite_program_source(source_text, agent_name), encoding="utf-8")
    return toolang_root, agent_name


@contextmanager
def materialized_run_target(
    toolang_root: Path,
    selector_text: str,
) -> Iterator[tuple[Path, str]]:
    """Yield one runnable local target for one selector."""

    selector = parse_agent_selector(selector_text)
    if selector.form == "name":
        yield toolang_root, selector.name or ""
        return
    with tempfile.TemporaryDirectory(prefix="toolang-run-") as temp_dir:
        temp_root = Path(temp_dir)
        agent_name = selector.default_name()
        source_text = fetch_agent_ref(resolve_agent_selector_ref(selector))
        write_agent_program(temp_root, agent_name, source_text)
        yield temp_root, agent_name


def remove_agent(toolang_root: Path, agent_name: str) -> Path:
    """Remove one stopped agent and its local sandbox staging."""

    home = agent_home(toolang_root, agent_name)
    if not home.is_dir():
        raise FileNotFoundError(f"agent not found: {home}")
    status = get_agent_status(toolang_root, agent_name, ui_base_url="")
    if status is not None and status.status in {"running", "preparing", "starting"}:
        raise ValueError(f"agent is still active: {agent_name}")
    runtime_pids = agent_runtime_process_pids(toolang_root, agent_name)
    if runtime_pids:
        raise ValueError(f"agent is still active: {agent_name} (pid {', '.join(str(pid) for pid in runtime_pids)})")
    shutil.rmtree(home)
    sandbox_stage_dir = _sandbox_stage_dir(toolang_root, agent_name)
    if sandbox_stage_dir.exists():
        shutil.rmtree(sandbox_stage_dir)
    return home


def _clone_local_agent(toolang_root: Path, source_name: str, target_name: str) -> Path:
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


def _parse_agent_ref(text: str) -> AgentRef:
    if text.startswith(("http://", "https://")):
        github_ref = _github_agent_ref_from_url(text)
        if github_ref is not None:
            return github_ref
        ref = HttpAgentRef(url=text)
        _require_too_path(urlsplit(ref.url).path, text)
        return ref
    if text.startswith("github://"):
        return _parse_github_agent_ref(text)
    raise ValueError(f"unsupported agent ref: {text}")


def _parse_github_agent_ref(text: str) -> GitHubAgentRef:
    parsed = urlsplit(text)
    owner = parsed.netloc.strip()
    path_text = parsed.path.strip("/")
    if not owner or not path_text or "/" not in path_text:
        raise ValueError(f"invalid GitHub agent ref: {text}")
    repo, _, repo_path = path_text.partition("/")
    if not repo or not repo_path:
        raise ValueError(f"invalid GitHub agent ref: {text}")
    path = repo_path
    rev: str | None = None
    if "@" in repo_path:
        path, _, rev = repo_path.rpartition("@")
        if not path or not rev:
            raise ValueError(f"invalid GitHub agent ref: {text}")
    if rev is None:
        raise ValueError(f"GitHub agent ref must include @rev: {text}")
    _require_too_path(path, text)
    return GitHubAgentRef(owner=owner, repo=repo, path=path, rev=rev)


def _github_agent_ref_from_url(text: str) -> GitHubAgentRef | None:
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc == "github.com":
        return _github_agent_ref_from_web_url(parsed.path, text)
    if parsed.netloc == "raw.githubusercontent.com":
        return _github_agent_ref_from_raw_url(parsed.path, text)
    return None


def _github_agent_ref_from_web_url(path_text: str, original: str) -> GitHubAgentRef:
    parts = [part for part in path_text.strip("/").split("/") if part]
    if len(parts) < 5 or parts[2] != "blob":
        raise ValueError(f"invalid GitHub agent ref: {original}")
    owner, repo, _mode, rev = parts[:4]
    path = "/".join(parts[4:])
    if not owner or not repo or not rev or not path:
        raise ValueError(f"invalid GitHub agent ref: {original}")
    _require_too_path(path, original)
    return GitHubAgentRef(owner=owner, repo=repo, path=path, rev=rev)


def _github_agent_ref_from_raw_url(path_text: str, original: str) -> GitHubAgentRef:
    parts = [part for part in path_text.strip("/").split("/") if part]
    if len(parts) < 4:
        raise ValueError(f"invalid GitHub agent ref: {original}")
    owner, repo, rev = parts[:3]
    path = "/".join(parts[3:])
    if not owner or not repo or not rev or not path:
        raise ValueError(f"invalid GitHub agent ref: {original}")
    _require_too_path(path, original)
    return GitHubAgentRef(owner=owner, repo=repo, path=path, rev=rev)


def _resolve_github_agent_shorthand(owner: str, name: str, *, repo: str) -> GitHubAgentRef:
    for candidate in _github_agent_shorthand_candidates(owner, repo, name):
        if _github_agent_ref_exists(candidate):
            return candidate
    raise ValueError(f"could not resolve agent shorthand: {owner}/{repo}/{name}")


def _github_agent_shorthand_candidates(owner: str, repo: str, name: str) -> tuple[GitHubAgentRef, ...]:
    try:
        rev = _github_repo_default_branch(owner, repo)
    except ValueError:
        return ()
    return (
        GitHubAgentRef(owner=owner, repo=repo, path=f"agents/{name}.too", rev=rev),
        GitHubAgentRef(owner=owner, repo=repo, path=f"{name}.too", rev=rev),
    )


def _github_agent_ref_exists(ref: GitHubAgentRef) -> bool:
    request = Request(_github_raw_agent_url(ref), method="HEAD", headers={"User-Agent": "toolang/0.1"})
    try:
        with urlopen(request, timeout=30):
            return True
    except (HTTPError, URLError):
        return False


def _github_raw_agent_url(ref: GitHubAgentRef) -> str:
    rev = quote(ref.rev, safe="/")
    path = quote(ref.path.lstrip("/"), safe="/")
    return f"https://raw.githubusercontent.com/{ref.owner}/{ref.repo}/{rev}/{path}"


@lru_cache
def _github_repo_default_branch(owner: str, repo: str) -> str:
    api_url = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    request = Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "toolang/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not resolve GitHub default branch: {owner}/{repo}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("default_branch"), str):
        raise ValueError(f"unexpected GitHub repository response: {owner}/{repo}")
    return data["default_branch"]


def _require_too_path(path_text: str, text: str) -> None:
    if Path(path_text).suffix != ".too":
        raise ValueError(f"agent ref must point to a .too program: {text}")


def _fetch_http_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "toolang/0.1"})
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except (HTTPError, URLError) as exc:
        raise ValueError(f"could not fetch agent program: {url}") from exc


def _fetch_github_text(ref: GitHubAgentRef) -> str:
    path = quote(ref.path.lstrip("/"), safe="/")
    api_url = f"https://api.github.com/repos/{ref.owner}/{ref.repo}/contents/{path}"
    api_url = f"{api_url}?ref={quote(ref.rev, safe='')}"
    request = Request(
        api_url,
        headers={
            "Accept": "application/vnd.github.raw",
            "User-Agent": "toolang/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except (HTTPError, URLError) as exc:
        raise ValueError(f"could not fetch agent program: {ref.render()}") from exc


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
    models: Sequence[str] | None = None,
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
            "models": list(models or ()),
            "message": message,
        },
    )
    return path


def stop_runtime_state(
    toolang_root: Path,
    agent_name: str,
    *,
    expected_pid: int | None = None,
    expected_started_at: str | None = None,
) -> bool:
    """Mark one runtime state as stopped while keeping the last endpoint."""

    path = agent_runtime_state_path(toolang_root, agent_name)
    runtime_state = _load_runtime_state(path)
    if runtime_state is None:
        return False
    if expected_pid is not None and runtime_state.get("pid") != expected_pid:
        return False
    if expected_started_at is not None and runtime_state.get("started_at") != expected_started_at:
        return False
    runtime_state["status"] = "stopped"
    runtime_state["pid"] = None
    runtime_state["message"] = None
    runtime_state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save_runtime_state(path, runtime_state)
    return True


def stop_agent(
    toolang_root: Path,
    agent_name: str,
    *,
    sandbox_plugin: SandboxPlugin | None = None,
    force: bool = False,
) -> bool:
    """Stop one running agent and mark its runtime state as stopped."""

    runtime_state = load_runtime_state(toolang_root, agent_name)
    pid = runtime_state.get("pid") if runtime_state is not None else None
    pid_alive = isinstance(pid, int) and _pid_alive(pid)
    runtime_pids = (
        ()
        if pid_alive
        else agent_runtime_process_pids(toolang_root, agent_name)
    )
    if runtime_state is None and not runtime_pids:
        raise FileNotFoundError(f"runtime state not found: {agent_runtime_state_path(toolang_root, agent_name)}")

    sandbox = runtime_state.get("sandbox") if runtime_state is not None else None
    started_at = runtime_state.get("started_at") if runtime_state is not None else None
    stopped = False
    if isinstance(sandbox, dict):
        if sandbox_plugin is None:
            raise ValueError("sandbox plugin is required to stop a sandboxed agent")
        sandbox_state = SandboxState.from_data(sandbox)
        if sandbox_state.runtime_id:
            sandbox_plugin.stop(sandbox_state, force=force)
            stopped = True
    failed_pids: list[int] = []
    if pid_alive and isinstance(pid, int):
        if _stop_pid(pid, force=force):
            stopped = True
        else:
            failed_pids.append(pid)
    for runtime_pid in runtime_pids:
        if runtime_pid == pid:
            continue
        if _stop_pid(runtime_pid, force=force):
            stopped = True
        else:
            failed_pids.append(runtime_pid)

    if failed_pids:
        pid_text = ", ".join(str(item) for item in sorted(set(failed_pids)))
        raise ValueError(f"agent did not stop: {agent_name} (pid {pid_text}); retry with --force")

    if runtime_state is not None:
        stop_runtime_state(
            toolang_root,
            agent_name,
            expected_pid=pid if isinstance(pid, int) else None,
            expected_started_at=started_at if isinstance(started_at, str) else None,
        )
    return stopped


def preferred_runtime_port(toolang_root: Path, agent_name: str) -> int | None:
    """Return one previously used port for an agent when available."""

    runtime_state = _load_runtime_state(agent_runtime_state_path(toolang_root, agent_name))
    return _runtime_state_port(runtime_state)


def assigned_runtime_ports(
    toolang_root: Path,
    *,
    exclude_agent: str | None = None,
) -> set[int]:
    """Return ports already recorded by local agents, including stopped ones."""

    agents_dir = toolang_root / "agents"
    if not agents_dir.is_dir():
        return set()
    ports: set[int] = set()
    for home in agents_dir.iterdir():
        if not home.is_dir():
            continue
        agent_name = home.name
        if exclude_agent is not None and agent_name == exclude_agent:
            continue
        port = _runtime_state_port(_load_runtime_state(agent_runtime_state_path(toolang_root, agent_name)))
        if port is not None:
            ports.add(port)
    return ports


def _runtime_state_port(runtime_state: dict[str, object] | None) -> int | None:
    """Return one parsed port from runtime state when available."""

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
    should_scan_processes = runtime_state is not None and raw_status == "stopped" and not pid_alive
    runtime_process_alive = pid_alive or bool(
        agent_runtime_process_pids(toolang_root, agent_name) if should_scan_processes else ()
    )
    sandbox_alive = _sandbox_alive(sandbox)
    status = _runtime_status_label(raw_status, pid_alive=runtime_process_alive, sandbox_alive=sandbox_alive)
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


def agent_runtime_process_pids(toolang_root: Path, agent_name: str) -> tuple[int, ...]:
    """Return live local runtime process ids for one agent.

    Runtime state is the normal source of truth, but older or interrupted
    remove flows can leave an orphan runtime process that continues to recreate
    prepared files. Detect those processes before deleting or recreating homes.
    """

    try:
        completed = subprocess.run(
            ("ps", "-axo", "pid=,command="),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ()

    expected_root = _resolved_path_text(toolang_root)
    pids: list[int] = []
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        pid_text, _, command = line.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == os.getpid() or not _pid_alive(pid):
            continue
        if _runtime_command_matches(command, root=expected_root, agent_name=agent_name):
            pids.append(pid)
    return tuple(sorted(set(pids)))


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


def _runtime_command_matches(command: str, *, root: str, agent_name: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return False

    raw_root = _option_value(tokens, "--root")
    if raw_root is None or _resolved_path_text(Path(raw_root)) != root:
        return False

    raw_agent = _option_value(tokens, "--agent")
    if raw_agent == agent_name:
        return True

    for index, token in enumerate(tokens[:-1]):
        if token == "run" and tokens[index + 1] == agent_name:
            return True
    return False


def _option_value(tokens: Sequence[str], option: str) -> str | None:
    prefix = f"{option}="
    for index, token in enumerate(tokens):
        if token.startswith(prefix):
            return token.removeprefix(prefix)
        if token == option and index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def _resolved_path_text(path: Path) -> str:
    return str(path.expanduser().resolve())


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _stop_pid(pid: int, *, force: bool) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError as exc:
        raise ValueError(f"permission denied while stopping pid {pid}") from exc

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    if not _pid_alive(pid):
        return True
    if not force:
        return False
    kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
    try:
        os.kill(pid, kill_signal)
    except ProcessLookupError:
        return True
    except PermissionError as exc:
        raise ValueError(f"permission denied while force-stopping pid {pid}") from exc
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


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
