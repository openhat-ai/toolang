"""Managed-agent file operations and agent source resolution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from collections.abc import Sequence
from typing import Literal

from toolang.common.github import (
    GitHubRef,
    github_raw_url,
    parse_github_file_url,
    parse_github_ref,
)
from toolang.common.files import atomic_write_text, file_write_lock
from toolang.common.layout import AgentLayout
from ..common.progress import ProgressSink, emit_progress

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


AgentRef = HttpAgentRef | GitHubRef


@dataclass(frozen=True, slots=True)
class AgentSelector:
    """One parsed resident or remote agent selector."""

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


def parse_agent_selector(text: str) -> AgentSelector:
    """Parse one resident name, shorthand, or canonical remote ref."""

    raw = text.strip()
    if not raw:
        raise ValueError("agent selector cannot be empty")
    if raw.startswith("agent:") and not raw.startswith("agent://"):
        name = raw.removeprefix("agent:").strip()
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError(f"invalid resident agent selector: {text}")
        return AgentSelector(form="name", text=raw, name=name)
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


def fetch_agent_ref(
    ref: AgentRef,
    *,
    progress: ProgressSink | None = None,
) -> str:
    """Fetch one remote authored agent program by canonical ref."""

    detail = ref.render()
    url = ref.url if isinstance(ref, HttpAgentRef) else github_raw_url(ref)
    emit_progress(
        progress,
        id=f"agent.fetch:{detail}",
        phase="agent.fetch",
        label="Fetch agent",
        status="running",
        detail=url,
    )
    try:
        source = _fetch_http_text(url)
    except Exception as exc:
        emit_progress(
            progress,
            id=f"agent.fetch:{detail}",
            phase="agent.fetch",
            label="Fetch agent",
            status="failed",
            detail=str(exc),
        )
        raise
    emit_progress(
        progress,
        id=f"agent.fetch:{detail}",
        phase="agent.fetch",
        label="Fetch agent",
        status="ok",
    )
    return source


def resolve_agent_selector_ref(
    selector: AgentSelector,
    *,
    progress: ProgressSink | None = None,
) -> AgentRef:
    """Resolve one remote selector to one canonical authored ref."""

    if selector.ref is not None:
        return selector.ref
    if selector.github_owner is None or selector.name is None:
        raise ValueError(f"selector is not a remote ref: {selector.text}")
    emit_progress(
        progress,
        id=f"agent.resolve:{selector.text}",
        phase="agent.resolve",
        label="Resolve agent",
        status="running",
        detail=selector.text,
    )
    try:
        ref = _resolve_github_agent_shorthand(
            selector.github_owner,
            selector.name,
            repo=selector.github_repo or "agents",
        )
    except Exception as exc:
        emit_progress(
            progress,
            id=f"agent.resolve:{selector.text}",
            phase="agent.resolve",
            label="Resolve agent",
            status="failed",
            detail=str(exc),
        )
        raise
    emit_progress(
        progress,
        id=f"agent.resolve:{selector.text}",
        phase="agent.resolve",
        label="Resolve agent",
        status="ok",
        detail=ref.render(),
    )
    return ref


@dataclass(frozen=True, slots=True)
class AgentStatus:
    """One listed agent status row."""

    name: str
    status: str
    endpoint: str | None
    api_url: str | None
    webui_url: str | None
    sandbox: str | None

    @property
    def port(self) -> int | None:
        """Return the runtime endpoint port when one is known."""

        if self.endpoint is None:
            return None
        try:
            return urlsplit(self.endpoint).port
        except ValueError:
            return None


class AgentProcess:
    """Inspect one resident AgentServer process."""

    def __init__(self, layout: AgentLayout) -> None:
        self.layout = layout

    def state(self) -> dict[str, object] | None:
        return _load_runtime_state(self.layout.runtime_status)

    def status(self, *, ui_base_url: str) -> AgentStatus | None:
        if not self.layout.home.is_dir():
            return None
        runtime_state = self.state()
        raw_endpoint = runtime_state.get("endpoint") if runtime_state else None
        raw_status = runtime_state.get("status") if runtime_state else None
        pid = runtime_state.get("pid") if runtime_state else None
        sandbox = _runtime_sandbox_label(runtime_state)
        endpoint = (
            raw_endpoint
            if isinstance(raw_endpoint, str) and raw_endpoint.strip()
            else None
        )
        pid_alive = isinstance(pid, int) and sandbox == "none" and _pid_alive(pid)
        scan = runtime_state is not None and raw_status == "stopped" and not pid_alive
        process_alive = pid_alive or bool(self.pids() if scan else ())
        status = _runtime_status_label(
            raw_status,
            pid_alive=process_alive,
            sandbox_alive=_hosting_running(self.layout),
        )
        active = status in {"running", "preparing", "starting"}
        return AgentStatus(
            name=self.layout.name,
            status=status,
            endpoint=endpoint if active else None,
            api_url=_api_docs_url(endpoint) if active else None,
            webui_url=(
                _webui_url(endpoint, ui_base_url=ui_base_url)
                if status == "running"
                else None
            ),
            sandbox=_runtime_sandbox_label(runtime_state),
        )

    @classmethod
    def list(cls, root: Path, *, ui_base_url: str) -> tuple[AgentStatus, ...]:
        agents_dir = root / "agents"
        if not agents_dir.is_dir():
            return ()
        statuses = (
            cls(AgentLayout.resident(root, home.name)).status(ui_base_url=ui_base_url)
            for home in sorted(item for item in agents_dir.iterdir() if item.is_dir())
        )
        return tuple(status for status in statuses if status is not None)

    def pids(self) -> tuple[int, ...]:
        return _agent_runtime_process_pids(self.layout)


VISITING_PROGRAM_CACHE_TTL_SEC = 3600


def remove_sandbox_stage(layout: AgentLayout) -> None:
    """Remove one agent's runtime-owned sandbox staging directory."""

    if layout.sandbox_stage.exists():
        shutil.rmtree(layout.sandbox_stage)


def materialize_roaming_program(source_path: Path) -> AgentLayout:
    """Materialize one local .too source into its fixed roaming root."""

    resolved_source = source_path.expanduser().resolve()
    if not resolved_source.is_file():
        raise FileNotFoundError(f"agent program not found: {resolved_source}")
    if resolved_source.suffix != ".too":
        raise ValueError(f"agent program must point to a .too file: {resolved_source}")
    layout = AgentLayout.roaming(resolved_source)
    layout.home.mkdir(parents=True, exist_ok=True)
    _replace_relative_symlink(
        layout.program,
        resolved_source,
    )
    _sync_roaming_config_link(layout.home, resolved_source.parent / "toolang.toml")
    return layout


def _sync_roaming_config_link(home: Path, source_config: Path) -> None:
    target = home / "config.toml"
    if source_config.is_file():
        _replace_relative_symlink(target, source_config, replace_regular_file=False)
    elif target.is_symlink():
        target.unlink()


def _replace_relative_symlink(
    link_path: Path, target_path: Path, *, replace_regular_file: bool = True
) -> None:
    if link_path.exists() or link_path.is_symlink():
        if not link_path.is_symlink() and not replace_regular_file:
            return
        if link_path.is_dir() and not link_path.is_symlink():
            raise IsADirectoryError(
                f"cannot replace directory with symlink: {link_path}"
            )
        link_path.unlink()
    link_path.parent.mkdir(parents=True, exist_ok=True)
    relative_target = os.path.relpath(target_path, start=link_path.parent)
    link_path.symlink_to(relative_target)


def materialize_visiting_program(
    ref: AgentRef,
    source_text: str,
    *,
    source: str | None = None,
) -> AgentLayout:
    """Materialize one remote agent into its stable visiting root."""

    agent_name = ref.default_name()
    layout = AgentLayout.visiting(source or ref.render(), agent_name)
    layout.home.mkdir(parents=True, exist_ok=True)
    layout.program.write_text(source_text, encoding="utf-8")
    return layout


def resolve_run_layout(
    toolang_root: Path,
    selector_text: str,
    *,
    progress: ProgressSink | None = None,
) -> AgentLayout:
    """Resolve and materialize one selector as an immutable agent layout."""

    selector = parse_agent_selector(selector_text)
    if selector.form == "name":
        return AgentLayout.resident(toolang_root, selector.name or "")

    return _resolve_visiting_layout(selector, progress=progress)


def visiting_layout(selector_text: str) -> AgentLayout:
    """Derive one visiting layout without resolving or fetching its source."""

    selector = parse_agent_selector(selector_text)
    if selector.form == "name":
        raise ValueError(f"agent selector is not remote: {selector_text}")
    return _visiting_layout(selector)


def resolve_visiting_layout(
    selector_text: str,
    *,
    progress: ProgressSink | None = None,
) -> AgentLayout:
    """Resolve and materialize one remote selector as a visiting layout."""

    selector = parse_agent_selector(selector_text)
    if selector.form == "name":
        raise ValueError(f"agent selector is not remote: {selector_text}")
    return _resolve_visiting_layout(selector, progress=progress)


def _visiting_layout(selector: AgentSelector) -> AgentLayout:
    return AgentLayout.visiting(selector.text, selector.default_name())


def _resolve_visiting_layout(
    selector: AgentSelector,
    *,
    progress: ProgressSink | None,
) -> AgentLayout:
    layout = _visiting_layout(selector)

    if _visiting_program_cache_fresh(layout.program):
        return layout
    resolved_ref = resolve_agent_selector_ref(selector, progress=progress)
    source_text = fetch_agent_ref(resolved_ref, progress=progress)
    emit_progress(
        progress,
        id=f"agent.materialize:{resolved_ref.render()}",
        phase="agent.materialize",
        label="Materialize agent",
        status="running",
        detail=layout.name,
    )
    layout = materialize_visiting_program(
        resolved_ref,
        source_text,
        source=selector.text,
    )
    emit_progress(
        progress,
        id=f"agent.materialize:{resolved_ref.render()}",
        phase="agent.materialize",
        label="Materialize agent",
        status="ok",
        detail=layout.name,
    )
    return layout


def _parse_agent_ref(text: str) -> AgentRef:
    if text.startswith(("http://", "https://")):
        github_ref = parse_github_file_url(text)
        if github_ref is not None:
            _require_too_path(github_ref.path, text)
            return github_ref
        ref = HttpAgentRef(url=text)
        _require_too_path(urlsplit(ref.url).path, text)
        return ref
    if text.startswith("github://"):
        github_ref = parse_github_ref(text)
        _require_too_path(github_ref.path, text)
        return github_ref
    raise ValueError(f"unsupported agent ref: {text}")


def _resolve_github_agent_shorthand(
    owner: str,
    name: str,
    *,
    repo: str,
) -> GitHubRef:
    for candidate in _github_agent_shorthand_candidates(owner, repo, name):
        if _github_agent_ref_exists(candidate):
            return candidate
    label = f"{owner}/{name}" if repo == "agents" else f"{owner}/{repo}/{name}"
    raise ValueError(f"could not resolve agent shorthand: {label}")


def _github_agent_shorthand_candidates(
    owner: str,
    repo: str,
    name: str,
) -> tuple[GitHubRef, ...]:
    try:
        rev = _github_repo_default_branch(owner, repo)
    except ValueError:
        rev = "main"
    return (
        GitHubRef(owner=owner, repo=repo, path=f"agents/{name}.too", rev=rev),
        GitHubRef(owner=owner, repo=repo, path=f"{name}.too", rev=rev),
    )


def _github_agent_ref_exists(ref: GitHubRef) -> bool:
    request = Request(
        github_raw_url(ref),
        method="HEAD",
        headers={"User-Agent": "toolang/0.1"},
    )
    try:
        with urlopen(request, timeout=30):
            return True
    except (HTTPError, URLError):
        return False


@lru_cache
def _github_repo_default_branch(owner: str, repo: str) -> str:
    api_url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    )
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
        raise ValueError(
            f"could not resolve GitHub default branch: {owner}/{repo}"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("default_branch"), str):
        raise ValueError(f"unexpected GitHub repository response: {owner}/{repo}")
    return data["default_branch"]


def _require_too_path(path: str, text: str) -> None:
    if Path(path).suffix != ".too":
        raise ValueError(f"agent ref must point to a .too program: {text}")


def _fetch_http_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "toolang/0.1"})
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except (HTTPError, URLError) as exc:
        raise ValueError(f"could not fetch agent program: {url}") from exc


def _visiting_program_cache_fresh(program_path: Path) -> bool:
    if not program_path.is_file():
        return False
    return time.time() - program_path.stat().st_mtime <= VISITING_PROGRAM_CACHE_TTL_SEC


def write_runtime_state(
    layout: AgentLayout,
    *,
    endpoint: str,
    started_at: str,
    pid: int | None,
    sandbox: str = "none",
    models: Sequence[str] | None = None,
    status: str = "running",
    message: str | None = None,
) -> Path:
    """Persist one minimal runtime state file for a running agent."""

    path = layout.runtime_status
    _save_runtime_state(
        path,
        {
            "agent": layout.name,
            "status": status,
            "endpoint": endpoint,
            "started_at": started_at,
            "updated_at": started_at,
            "pid": pid,
            "sandbox": sandbox,
            "models": list(models or ()),
            "message": message,
        },
    )
    return path


def stop_runtime_state(
    layout: AgentLayout,
    *,
    expected_pid: int | None = None,
    expected_started_at: str | None = None,
) -> bool:
    """Mark one runtime state as stopped while keeping the last endpoint."""

    path = layout.runtime_status
    with file_write_lock(path.with_suffix(".lock")):
        runtime_state = _load_runtime_state(path)
        if runtime_state is None:
            return False
        if expected_pid is not None and runtime_state.get("pid") != expected_pid:
            return False
        if (
            expected_started_at is not None
            and runtime_state.get("started_at") != expected_started_at
        ):
            return False
        runtime_state["status"] = "stopped"
        runtime_state["pid"] = None
        runtime_state["message"] = None
        runtime_state["updated_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(),
        )
        _save_runtime_state(path, runtime_state)
    return True


def preferred_runtime_port(layout: AgentLayout) -> int | None:
    """Return one previously used port for an agent when available."""

    runtime_state = _load_runtime_state(layout.runtime_status)
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
        layout = AgentLayout.resident(toolang_root, agent_name)
        port = _runtime_state_port(_load_runtime_state(layout.runtime_status))
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


def runtime_pid_label(
    runtime_state: dict[str, object] | None,
    *,
    layout: AgentLayout | None = None,
) -> str | None:
    """Return one human-readable process label for runtime info output."""

    if runtime_state is None:
        return None
    if layout is not None:
        from toolang.up.hosting import HostingState

        state = HostingState.load(layout.hosting_state)
        if state is not None:
            return state.ref.runtime_id
    pid = runtime_state.get("pid")
    if isinstance(pid, int) and pid > 0:
        return str(pid)
    return None


def _agent_runtime_process_pids(layout: AgentLayout) -> tuple[int, ...]:
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

    expected_root = _resolved_path_text(layout.root)
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
        if _runtime_command_matches(
            command,
            root=expected_root,
            agent_name=layout.name,
        ):
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
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _runtime_status_label(
    raw_status: object, *, pid_alive: bool, sandbox_alive: bool
) -> str:
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
    if isinstance(sandbox, str) and sandbox.strip():
        return sandbox.strip()
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
        if token == "serve" and tokens[index + 1] == agent_name:
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


def _hosting_running(layout: AgentLayout) -> bool:
    from toolang.up.hosting import HostingState
    from toolang.plugin.sandboxes.loading import load_hosting

    try:
        state = HostingState.load(layout.hosting_state)
    except ValueError:
        return False
    if state is None:
        return False
    name, _, _ = state.sandbox.partition(":")
    try:
        return asyncio.run(load_hosting(name, config={}).running(state.ref))
    except (OSError, RuntimeError, ValueError):
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
