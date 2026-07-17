"""Authored resident agent catalog and source resolution."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import shutil
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from toolang.common.github import (
    GitHubRef,
    github_raw_url,
    parse_github_file_url,
    parse_github_ref,
)
from toolang.common.progress import ProgressSink, emit_progress

AgentSelectorForm = Literal["name", "shorthand", "ref"]
DEFAULT_AGENT_SOURCE = (
    "# Customize this agent here.\n"
    "# Docs: https://toolang.ai/docs\n"
)


@dataclass(frozen=True, slots=True)
class AgentLayout:
    """Authored filesystem placement for one resident agent."""

    root: Path
    name: str
    home: Path
    program: Path


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


def agent_home(toolang_root: Path, agent_name: str) -> Path:
    """Return one authored resident agent home path."""

    return toolang_root / "agents" / agent_name


def agent_program_path(toolang_root: Path, agent_name: str) -> Path:
    """Return one authored resident agent program path."""

    return agent_home(toolang_root, agent_name) / "agent.too"


def parse_agent_selector(text: str) -> AgentSelector:
    """Parse one resident name, shorthand, or canonical remote ref."""

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


def fetch_agent_ref(
    ref: AgentRef,
    *,
    progress: ProgressSink | None = None,
) -> str:
    """Fetch one remote authored agent program by canonical ref."""

    label = "Fetch agent"
    detail = ref.render()
    fetch_url = ref.url if isinstance(ref, HttpAgentRef) else github_raw_url(ref)
    emit_progress(
        progress,
        id=f"agent.fetch:{detail}",
        phase="agent.fetch",
        label=label,
        status="running",
        detail=fetch_url,
    )
    try:
        source = (
            _fetch_http_text(ref.url)
            if isinstance(ref, HttpAgentRef)
            else _fetch_http_text(github_raw_url(ref))
        )
    except Exception as exc:
        emit_progress(
            progress,
            id=f"agent.fetch:{detail}",
            phase="agent.fetch",
            label=label,
            status="failed",
            detail=str(exc),
        )
        raise
    emit_progress(
        progress,
        id=f"agent.fetch:{detail}",
        phase="agent.fetch",
        label=label,
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
    label = "Resolve agent"
    detail = selector.text
    emit_progress(
        progress,
        id=f"agent.resolve:{detail}",
        phase="agent.resolve",
        label=label,
        status="running",
        detail=detail,
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
            id=f"agent.resolve:{detail}",
            phase="agent.resolve",
            label=label,
            status="failed",
            detail=str(exc),
        )
        raise
    emit_progress(
        progress,
        id=f"agent.resolve:{detail}",
        phase="agent.resolve",
        label=label,
        status="ok",
        detail=ref.render(),
    )
    return ref


class AgentCatalog:
    """CRUD over authored resident agent directories under one Toolang root."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def list(self) -> tuple[AgentLayout, ...]:
        directory = self.root / "agents"
        if not directory.is_dir():
            return ()
        return tuple(
            self._layout(path.name)
            for path in sorted(directory.iterdir())
            if path.is_dir() and (path / "agent.too").is_file()
        )

    def get(self, name: str) -> AgentLayout | None:
        layout = self._layout(name)
        return layout if layout.program.is_file() else None

    def create(
        self,
        name: str,
        *,
        source_text: str = DEFAULT_AGENT_SOURCE,
    ) -> AgentLayout:
        program = write_agent_program(self.root, name, source_text)
        return self._layout(program.parent.name)

    def clone(self, source: str, name: str | None = None) -> AgentLayout:
        selector = parse_agent_selector(source)
        if selector.form == "name":
            if name is None:
                raise ValueError("target name is required when cloning one local agent")
            program = self._clone_local(selector.name or "", name)
        else:
            ref = resolve_agent_selector_ref(selector)
            target = name or selector.default_name()
            program = write_agent_program(self.root, target, fetch_agent_ref(ref))
        return self._layout(program.parent.name)

    def remove(self, name: str) -> AgentLayout:
        layout = self.get(name)
        if layout is None:
            raise FileNotFoundError(f"agent not found: {agent_home(self.root, name)}")
        shutil.rmtree(layout.home)
        return layout

    def _clone_local(self, source_name: str, target_name: str) -> Path:
        source_home = agent_home(self.root, source_name)
        target_home = agent_home(self.root, target_name)
        if not source_home.is_dir():
            raise FileNotFoundError(f"source agent not found: {source_home}")
        if target_home.exists():
            raise FileExistsError(f"target agent already exists: {target_home}")
        shutil.copytree(
            source_home,
            target_home,
            ignore=shutil.ignore_patterns(".caps", ".runtime"),
        )
        target_program = target_home / "agent.too"
        source_text = target_program.read_text(encoding="utf-8")
        target_program.write_text(
            normalize_agent_source(source_text, target_name),
            encoding="utf-8",
        )
        return target_program

    def _layout(self, name: str) -> AgentLayout:
        return AgentLayout(
            root=self.root,
            name=name,
            home=agent_home(self.root, name),
            program=agent_program_path(self.root, name),
        )


def write_agent_program(
    toolang_root: Path,
    agent_name: str,
    source_text: str,
) -> Path:
    """Create one local authored agent program from explicit source text."""

    home = agent_home(toolang_root, agent_name)
    if home.exists():
        raise FileExistsError(f"agent already exists: {home}")
    home.mkdir(parents=True, exist_ok=False)
    program_path = agent_program_path(toolang_root, agent_name)
    program_path.write_text(
        normalize_agent_source(source_text, agent_name),
        encoding="utf-8",
    )
    return program_path


def normalize_agent_source(source_text: str, agent_name: str) -> str:
    """Return authored source normalized for one local agent name."""

    lines = source_text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip().startswith("agent "):
            suffix = "\n" if line.endswith("\n") else ""
            lines[index] = f"agent {agent_name}{suffix}"
            break
    return "".join(lines)


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
