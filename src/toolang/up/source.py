"""Resolve and materialize local or remote agent Mod sources."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
from stat import S_ISLNK
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen
from typing import Literal

from toolang.common.files import EditLock, atomic_write_text
from toolang.common.github import GitHubRef, GitHubShorthand, github_raw_url
from toolang.common.layout import AgentLayout, derive_agent_name, validate_agent_name
from toolang.common.progress import ProgressSink, emit_progress
from toolang.common.source import HttpRef, parse_remote_source

AgentTargetForm = Literal["name", "shorthand", "ref"]
_MAX_ROAMING_PROJECTION_ATTEMPTS = 3


AgentRef = HttpRef | GitHubRef


@dataclass(frozen=True, slots=True)
class AgentTarget:
    """One parsed resident or remote agent target."""

    form: AgentTargetForm
    text: str
    name: str | None = None
    ref: AgentRef | None = None
    github_owner: str | None = None
    github_repo: str | None = None

    def resolved_ref(self) -> AgentRef:
        if self.ref is None:
            raise ValueError(f"target is not a remote ref: {self.text}")
        return self.ref

    def default_name(self) -> str:
        if self.name is not None:
            return derive_agent_name(self.name)
        return derive_agent_name(self.resolved_ref().default_name())


def parse_agent_target(text: str) -> AgentTarget:
    """Parse an explicit resident Agent Name or remote Source Ref."""
    if text.startswith("agent:"):
        return AgentTarget(
            form="name",
            text=text,
            name=validate_agent_name(text.removeprefix("agent:")),
        )
    raw = text
    if ":" not in raw and "/" not in raw:
        return AgentTarget(form="name", text=text, name=validate_agent_name(raw))
    source = parse_remote_source(raw)
    if isinstance(source, GitHubShorthand):
        return AgentTarget(
            form="shorthand",
            text=source.render(),
            name=source.name,
            github_owner=source.owner,
            github_repo=source.repo,
        )
    path = source.path if isinstance(source, GitHubRef) else urlsplit(source.url).path
    _require_too_path(unquote(path), raw)
    identity = (
        source.render() if raw.strip().startswith(("gh:", "github:")) else raw.strip()
    )
    return AgentTarget(form="ref", text=identity, ref=source)


def fetch_agent_ref(
    ref: AgentRef,
    *,
    progress: ProgressSink | None = None,
    progress_id: str | None = None,
) -> str:
    """Fetch one remote authored agent program by canonical ref."""

    detail = ref.render()
    item_id = progress_id or f"agent:{detail}"
    url = ref.url if isinstance(ref, HttpRef) else github_raw_url(ref)
    emit_progress(
        progress,
        id=item_id,
        kind="prepare",
        stage="fetch",
        label="Fetching agent...",
        status="running",
        detail=url,
        subject="agent",
        ref=detail,
    )
    try:
        source = _fetch_http_text(url)
    except Exception as exc:
        emit_progress(
            progress,
            id=item_id,
            kind="prepare",
            stage="fetch",
            label="Failed to fetch agent",
            status="failed",
            detail=str(exc),
            subject="agent",
            ref=detail,
            complete=True,
        )
        raise
    emit_progress(
        progress,
        id=item_id,
        kind="prepare",
        stage="fetch",
        label="Fetched agent",
        status="ok",
        subject="agent",
        ref=detail,
        complete=True,
    )
    return source


def resolve_agent_source(
    target: AgentTarget,
    *,
    progress: ProgressSink | None = None,
    progress_id: str | None = None,
) -> AgentRef:
    """Resolve one remote target to one canonical authored ref."""

    if target.ref is not None:
        return target.ref
    if target.github_owner is None or target.name is None:
        raise ValueError(f"target is not a remote ref: {target.text}")
    item_id = progress_id or f"agent:{target.text}"
    emit_progress(
        progress,
        id=item_id,
        kind="prepare",
        stage="resolve",
        label="Resolving agent...",
        status="running",
        detail=target.text,
        subject="agent",
        ref=target.text,
    )
    try:
        ref = _resolve_github_agent_shorthand(
            target.github_owner,
            target.name,
            repo=target.github_repo or "agents",
        )
    except Exception as exc:
        emit_progress(
            progress,
            id=item_id,
            kind="prepare",
            stage="resolve",
            label="Failed to resolve agent",
            status="failed",
            detail=str(exc),
            subject="agent",
            ref=target.text,
            complete=True,
        )
        raise
    emit_progress(
        progress,
        id=item_id,
        kind="prepare",
        stage="resolve",
        label="Resolved agent",
        status="ok",
        detail=ref.render(),
        subject="agent",
        ref=ref.render(),
        complete=True,
    )
    return ref


VISITING_PROGRAM_CACHE_TTL_SEC = 3600


def roaming_layout(source_path: Path) -> AgentLayout:
    """Resolve a roaming home without reading another source's state or writing."""
    source = source_path.expanduser().resolve()
    layout = AgentLayout.roaming(source)
    try:
        mode = layout.program.lstat().st_mode
    except FileNotFoundError:
        return layout
    if S_ISLNK(mode):
        owner = layout.program.resolve()
        if owner != source:
            raise FileExistsError(
                f"agent home {layout.home} already belongs to {owner}"
            )
    else:
        raise FileExistsError(f"cannot replace authored file: {layout.program}")
    return layout


def materialize_roaming_program(source_path: Path) -> AgentLayout:
    """Materialize one local .too source into its fixed roaming root."""

    resolved_source = source_path.expanduser().resolve()
    if not resolved_source.is_file():
        raise FileNotFoundError(f"agent program not found: {resolved_source}")
    if resolved_source.suffix != ".too":
        raise ValueError(f"agent program must point to a .too file: {resolved_source}")
    layout = roaming_layout(resolved_source)
    _link_roaming_program(layout, resolved_source)
    _sync_roaming_project(layout, resolved_source)
    return layout


def _link_roaming_program(layout: AgentLayout, source: Path) -> None:
    """Atomically claim the normalized home without replacing another source."""
    layout.home.mkdir(parents=True, exist_ok=True)
    try:
        layout.program.symlink_to(os.path.relpath(source, start=layout.home))
    except FileExistsError:
        if layout.program.is_symlink():
            if layout.program.resolve() == source:
                return
            raise FileExistsError(
                f"agent home {layout.home} already belongs to {layout.program.resolve()}"
            ) from None
        raise FileExistsError(
            f"cannot replace authored file: {layout.program}"
        ) from None


def _sync_roaming_project(layout: AgentLayout, source: Path) -> None:
    from toolang.common.config_sources import (
        PROJECTION_HEADER,
        project_sources,
        read_config,
        script_sources,
        source_catalog,
    )
    from toolang.common.files import atomic_write_text

    home = layout.home
    root_lock = EditLock(layout.root_edit_lock)

    def capture():
        sources = script_sources(source)
        return sources, source_catalog(sources, roaming=True, validate=False)

    sources, catalog = root_lock.read(capture)
    projected = project_sources(sources, source=source, catalog=catalog)
    target = home / "config.toml"
    catalog_target = home / "catalog.json"
    lock = EditLock(layout.home_edit_lock)

    def needs_update() -> bool:
        previous_catalog = None
        previous_source = None
        if (
            target.is_file()
            and not target.is_symlink()
            and target.read_text().startswith(PROJECTION_HEADER)
        ):
            previous = read_config(target)
            previous_catalog = previous.get("__toolang_catalog__")
            previous_source = previous.get("__toolang_source__")
        for path in (target, catalog_target):
            if path.is_symlink():
                # Relative generated links remain owned when the project moves,
                # even if the old absolute catalog origin no longer matches.
                if (
                    path == catalog_target
                    and isinstance(previous_catalog, str)
                    and isinstance(previous_source, str)
                ):
                    old_source = Path(previous_source)
                    old_home = AgentLayout.roaming(old_source).home
                    if os.readlink(path) == os.path.relpath(
                        previous_catalog, start=old_home
                    ):
                        continue
                expected = (
                    (source.with_name("toolang.toml"),)
                    if path == target
                    else tuple(
                        value
                        for value in (
                            catalog,
                            Path(previous_catalog)
                            if isinstance(previous_catalog, str)
                            else None,
                        )
                        if value is not None
                    )
                )
                if any(path.resolve() == value.resolve() for value in expected):
                    continue
                raise ValueError(
                    f"generated project symlink conflicts with existing entry: {path}"
                )
            if not path.exists():
                continue
            if (
                path == target
                and path.is_file()
                and path.read_text().startswith(PROJECTION_HEADER)
            ):
                continue
            raise ValueError(
                f"generated project file conflicts with existing file: {path}"
            )
        return (
            target.is_symlink()
            or not target.exists()
            or target.read_text() != projected
            or (
                catalog is not None
                and (
                    not catalog_target.is_symlink()
                    or catalog_target.resolve() != catalog.resolve()
                )
            )
            or (catalog is None and catalog_target.is_symlink())
        )

    for _ in range(_MAX_ROAMING_PROJECTION_ATTEMPTS):
        if lock.read(needs_update):
            with lock.write():
                if needs_update():
                    if target.is_symlink():
                        target.unlink()
                    if not target.exists() or target.read_text() != projected:
                        atomic_write_text(target, projected)
                    if catalog is not None:
                        _replace_relative_symlink(
                            catalog_target, catalog, replace_regular_file=False
                        )
                    elif catalog_target.is_symlink():
                        catalog_target.unlink()
        # Root edits and home projection never hold each other's guards. An
        # older invocation delayed on the home guard must not leave a stale
        # projection behind after a newer root edit/projection has completed.
        current_sources, current_catalog = root_lock.read(capture)
        if (sources, catalog) == (current_sources, current_catalog):
            return
        sources, catalog = current_sources, current_catalog
        projected = project_sources(sources, source=source, catalog=catalog)
    raise RuntimeError("project sources changed repeatedly while projecting")


def _replace_relative_symlink(
    link_path: Path, target_path: Path, *, replace_regular_file: bool = True
) -> None:
    if link_path.is_symlink() and link_path.resolve() == target_path.resolve():
        return
    if link_path.exists() or link_path.is_symlink():
        if not link_path.is_symlink() and not replace_regular_file:
            raise FileExistsError(f"cannot replace authored file: {link_path}")
        if link_path.is_dir() and not link_path.is_symlink():
            raise IsADirectoryError(
                f"cannot replace directory with symlink: {link_path}"
            )
        link_path.unlink()
    link_path.parent.mkdir(parents=True, exist_ok=True)
    relative_target = os.path.relpath(target_path, start=link_path.parent)
    try:
        link_path.symlink_to(relative_target)
    except FileExistsError:
        # Another invocation may have just installed the identical owned link.
        if not link_path.is_symlink() or link_path.resolve() != target_path.resolve():
            raise


def materialize_visiting_program(
    ref: AgentRef,
    source_text: str,
    *,
    source: str | None = None,
    toolang_root: Path,
) -> AgentLayout:
    """Materialize one remote agent into its stable visiting root."""

    agent_name = derive_agent_name(ref.default_name())
    layout = AgentLayout.visiting(
        source or ref.render(), agent_name, toolang_root=toolang_root
    )
    atomic_write_text(layout.program, source_text)
    return layout


def resolve_run_layout(
    toolang_root: Path,
    target_text: str,
    *,
    progress: ProgressSink | None = None,
) -> AgentLayout:
    """Resolve and materialize one target as an immutable agent layout."""

    target = parse_agent_target(target_text)
    if target.form == "name":
        return AgentLayout.resident(toolang_root, target.name or "")

    return _resolve_visiting_layout(
        target, progress=progress, toolang_root=toolang_root
    )


def visiting_layout(target_text: str, *, toolang_root: Path) -> AgentLayout:
    """Derive one visiting layout without resolving or fetching its source."""

    target = parse_agent_target(target_text)
    if target.form == "name":
        raise ValueError(f"agent target is not remote: {target_text}")
    return _visiting_layout(target, toolang_root=toolang_root)


def resolve_visiting_layout(
    target_text: str,
    *,
    toolang_root: Path,
    progress: ProgressSink | None = None,
) -> AgentLayout:
    """Resolve and materialize one remote target as a visiting layout."""

    target = parse_agent_target(target_text)
    if target.form == "name":
        raise ValueError(f"agent target is not remote: {target_text}")
    return _resolve_visiting_layout(
        target, progress=progress, toolang_root=toolang_root
    )


def _visiting_layout(target: AgentTarget, *, toolang_root: Path) -> AgentLayout:
    return AgentLayout.visiting(
        target.text, target.default_name(), toolang_root=toolang_root
    )


def _resolve_visiting_layout(
    target: AgentTarget,
    *,
    progress: ProgressSink | None,
    toolang_root: Path,
) -> AgentLayout:
    from toolang.state.prepare import parse_visiting_agent

    layout = _visiting_layout(target, toolang_root=toolang_root)

    if _visiting_program_cache_fresh(layout.program):
        parse_visiting_agent(
            layout,
            ref=target.text,
            progress=progress,
            progress_id=f"agent:{target.text}",
        )
        return layout
    progress_id = f"agent:{target.text}"
    resolved_ref = resolve_agent_source(
        target,
        progress=progress,
        progress_id=progress_id,
    )
    source_text = fetch_agent_ref(
        resolved_ref,
        progress=progress,
        progress_id=progress_id,
    )
    parse_visiting_agent(
        layout,
        ref=resolved_ref.render(),
        source=source_text,
        progress=progress,
        progress_id=progress_id,
    )
    emit_progress(
        progress,
        id=progress_id,
        kind="prepare",
        stage="materialize",
        label="Caching agent source...",
        status="running",
        detail=layout.name,
    )
    try:
        layout = materialize_visiting_program(
            resolved_ref,
            source_text,
            source=target.text,
            toolang_root=toolang_root,
        )
    except Exception as exc:
        emit_progress(
            progress,
            id=progress_id,
            kind="prepare",
            stage="materialize",
            label="Failed to cache agent source",
            status="failed",
            detail=str(exc),
        )
        raise
    emit_progress(
        progress,
        id=progress_id,
        kind="prepare",
        stage="materialize",
        label="Cached agent source",
        status="ok",
        detail=layout.name,
    )
    return layout


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
