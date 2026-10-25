"""Resolve and materialize local or remote agent Mod sources."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from typing import TYPE_CHECKING, Literal

from toolang.common.files import EditLock, atomic_write_text
from toolang.common.github import (
    GitHubRef,
    GitHubShorthand,
    github_raw_url,
    pin_github_ref,
)
from toolang.common.layout import AgentLayout, derive_agent_name, validate_agent_name
from toolang.common.progress import ProgressSink, emit_progress
from toolang.common.source import (
    AcquiredSource,
    Acquisition,
    HttpRef,
    RemoteSource,
    parse_remote_source,
)
from .identity import visiting_home_guard

if TYPE_CHECKING:
    from .addresses import AgentAddress

AgentTargetForm = Literal["name", "shorthand", "ref"]
_MAX_ROAMING_PROJECTION_ATTEMPTS = 3


AgentRef = HttpRef | GitHubRef


def _display_ref(value: str) -> str:
    """Keep URL credentials, signed queries and fragments out of progress/errors."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        return value
    return parsed._replace(
        netloc=parsed.netloc.rsplit("@", 1)[-1], query="", fragment=""
    ).geturl()


@dataclass(frozen=True, slots=True)
class AgentTarget:
    """One parsed resident or remote agent target."""

    text: str
    name: str | None = None
    source: RemoteSource | None = None
    declared_ref: str | None = None

    @property
    def form(self) -> AgentTargetForm:
        if self.source is None:
            return "name"
        return "shorthand" if isinstance(self.source, GitHubShorthand) else "ref"

    def resolved_ref(self) -> AgentRef:
        if self.source is None or isinstance(self.source, GitHubShorthand):
            raise ValueError(f"target is not a remote ref: {self.text}")
        return self.source

    def default_name(self) -> str:
        if self.name is not None:
            return derive_agent_name(self.name)
        if isinstance(self.source, GitHubShorthand):
            return derive_agent_name(self.source.name)
        return derive_agent_name(self.resolved_ref().default_name())


def parse_agent_target(text: str) -> AgentTarget:
    """Parse an explicit resident Agent Name or remote Source Ref."""
    if text.startswith("agent:"):
        return AgentTarget(
            text=text,
            name=validate_agent_name(text.removeprefix("agent:")),
        )
    if ":" not in text and "/" not in text:
        return AgentTarget(text=text, name=validate_agent_name(text))
    source = parse_remote_source(text)
    if isinstance(source, GitHubShorthand):
        return AgentTarget(
            text=source.render(), source=source, declared_ref=text.strip()
        )
    _require_too_path(source.path, text)
    identity = source.render() if isinstance(source, GitHubRef) else text.strip()
    return AgentTarget(text=identity, source=source, declared_ref=text.strip())


def parse_agent_selector(text: str) -> AgentTarget | Path | AgentAddress:
    """Parse selector syntax without acquiring sources or homes."""
    from .addresses import parse_address

    if address := parse_address(text):
        return address

    if ":" not in text.split("/", 1)[0] and (
        text.endswith(".too") or text.startswith(("./", "../", "/", "~/"))
    ):
        path = Path(text).expanduser().resolve()
        if path.suffix != ".too":
            raise ValueError(f"agent program must point to a .too file: {text}")
        return path
    return parse_agent_target(text)


def fetch_agent_ref(
    ref: AgentRef,
    *,
    progress: ProgressSink | None = None,
    progress_id: str | None = None,
) -> str:
    """Fetch one remote authored agent program by canonical ref."""

    detail = _display_ref(ref.render())
    item_id = progress_id or f"agent:{detail}"
    url = ref.url if isinstance(ref, HttpRef) else github_raw_url(ref)
    emit_progress(
        progress,
        id=item_id,
        kind="prepare",
        stage="fetch",
        label="Fetching agent...",
        status="running",
        detail=_display_ref(url),
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

    source = target.source
    if not isinstance(source, GitHubShorthand):
        return target.resolved_ref()
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
        ref=_display_ref(target.text),
    )
    try:
        ref = _resolve_github_agent_shorthand(
            source.owner,
            source.name,
            repo=source.repo or "agents",
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
            ref=_display_ref(target.text),
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


def roaming_layout(source_path: Path, *, toolang_root: Path) -> AgentLayout:
    """Prepare a ROOT-scoped retained identity without starting a runtime."""
    from .allocation import prepare_roaming_home

    return prepare_roaming_home(source_path, toolang_root=toolang_root)


def materialize_roaming_program(
    source_path: Path,
    *,
    layout: AgentLayout | None = None,
    toolang_root: Path | None = None,
) -> AgentLayout:
    """Materialize one local .too source into its fixed roaming place."""

    resolved_source = source_path.expanduser().resolve()
    if not resolved_source.is_file():
        raise FileNotFoundError(f"agent program not found: {resolved_source}")
    if resolved_source.suffix != ".too":
        raise ValueError(f"agent program must point to a .too file: {resolved_source}")
    if layout is None:
        if toolang_root is None:
            raise ValueError("roaming preparation requires an explicit Toolang root")
        layout = roaming_layout(resolved_source, toolang_root=toolang_root)
    if (
        layout.source_path != resolved_source
        or layout.place_code is None
        or layout.root is None
    ):
        raise ValueError("roaming preparation requires a bound ROOT-scoped HOME")
    from .identity import read_home_identity

    read_home_identity(layout, toolang_root=layout.root)
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

    place = layout.place
    root_lock = EditLock(layout.place_edit_lock)

    def capture():
        sources = script_sources(source)
        return sources, source_catalog(sources, roaming=True, validate=False)

    sources, catalog = root_lock.read(capture)
    projected = project_sources(sources, source=source, catalog=catalog)
    target = place / "config.toml"
    catalog_target = place / "catalog.json"
    lock = EditLock(layout.place_state / "edit.lock")

    def retire_previous_projection() -> None:
        # Relocate generated inputs only after the new PLACE projection succeeds.
        old_config = layout.config
        old_lock = EditLock(layout.home_edit_lock)

        def owned() -> bool:
            if old_config.is_symlink():
                return (
                    old_config.resolve() == source.with_name("toolang.toml").resolve()
                )
            return old_config.is_file() and old_config.read_text().startswith(
                PROJECTION_HEADER
            )

        if not old_lock.read(owned):
            return
        with old_lock.write():
            if not owned():
                return
            previous = read_config(old_config)
            old_catalog = layout.home / "catalog.json"
            known_catalogs = {source.with_name("toolang.catalog.json").resolve()}
            for value in (previous.get("__toolang_catalog__"), catalog):
                if isinstance(value, str | Path):
                    known_catalogs.add(Path(value).resolve())
            if old_catalog.is_symlink() and old_catalog.resolve() in known_catalogs:
                old_catalog.unlink()
            old_config.unlink()

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
                    old_home = AgentLayout.roaming(old_source).place
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
                        _replace_relative_symlink(catalog_target, catalog)
                    elif catalog_target.is_symlink():
                        catalog_target.unlink()
        # Authored edits and projection never hold each other's guards. An
        # older invocation delayed on the projection guard must not leave a stale
        # projection behind after a newer root edit/projection has completed.
        current_sources, current_catalog = root_lock.read(capture)
        if (sources, catalog) == (current_sources, current_catalog):
            retire_previous_projection()
            return
        sources, catalog = current_sources, current_catalog
        projected = project_sources(sources, source=source, catalog=catalog)
    raise RuntimeError("project sources changed repeatedly while projecting")


def _replace_relative_symlink(link_path: Path, target_path: Path) -> None:
    if link_path.is_symlink() and link_path.resolve() == target_path.resolve():
        return
    if link_path.exists() or link_path.is_symlink():
        if not link_path.is_symlink():
            raise FileExistsError(f"cannot replace authored file: {link_path}")
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
    declared_ref: str | None = None,
    toolang_root: Path,
) -> AgentLayout:
    """Materialize one remote agent into its stable visiting place."""

    agent_name = derive_agent_name(ref.default_name())
    layout = AgentLayout.visiting(source or ref.render(), agent_name, root=toolang_root)
    content = source_text.encode("utf-8")
    acquired = AcquiredSource(
        declared_ref or source or ref.render(),
        Acquisition(
            ref.render(),
            ref.path.lstrip("/"),
            {ref.path.lstrip("/"): content},
            agent_name,
        ),
    )
    # This receipt belongs to the visiting source cache, outside State revisions.
    # Write it after the program; a mismatched pair is never reused.
    with visiting_home_guard(layout):
        atomic_write_text(layout.program, source_text)
        atomic_write_text(
            layout.home / "agent.source.json",
            json.dumps(
                {
                    "declared_ref": acquired.declared_ref,
                    "resolved_ref": acquired.acquisition.resolved_ref,
                    "entry": acquired.acquisition.entry,
                    "sha256": sha256(content).hexdigest(),
                },
                sort_keys=True,
            ),
        )
    return replace(layout, program_acquisition=acquired)


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


def prepare_visiting_layout(
    layout: AgentLayout,
    *,
    toolang_root: Path,
    source_ref: str | None = None,
    progress: ProgressSink | None = None,
) -> AgentLayout:
    """Prepare visiting inputs after startup validation; other sources are ready."""
    if layout.placement != "visiting" or layout.source_ref is None:
        return layout
    source_ref = source_ref or layout.source_ref
    if visiting_layout(source_ref, toolang_root=toolang_root).place != layout.place:
        raise ValueError("visiting source identity changed")
    return resolve_visiting_layout(
        source_ref, toolang_root=toolang_root, progress=progress
    )


def _visiting_layout(target: AgentTarget, *, toolang_root: Path) -> AgentLayout:
    return AgentLayout.visiting(target.text, target.default_name(), root=toolang_root)


def _resolve_visiting_layout(
    target: AgentTarget,
    *,
    progress: ProgressSink | None,
    toolang_root: Path,
) -> AgentLayout:
    layout = _visiting_layout(target, toolang_root=toolang_root)
    with visiting_home_guard(layout):
        return _prepare_visiting_source(
            target, layout, progress=progress, toolang_root=toolang_root
        )


def _prepare_visiting_source(
    target: AgentTarget,
    layout: AgentLayout,
    *,
    progress: ProgressSink | None,
    toolang_root: Path,
) -> AgentLayout:
    from toolang.state.prepare import parse_visiting_agent

    captured = _cached_visiting_source(layout)
    if (
        captured is not None
        and reusable_agent_source(target, captured.acquisition.resolved_ref)
        and _visiting_program_cache_fresh(layout.program)
    ):
        layout = replace(layout, program_acquisition=captured)
        parse_visiting_agent(
            layout,
            ref=_display_ref(target.text),
            progress=progress,
            progress_id=f"agent:{_display_ref(target.text)}",
        )
        return layout
    progress_id = f"agent:{_display_ref(target.text)}"
    resolved_ref = resolve_agent_source(
        target,
        progress=progress,
        progress_id=progress_id,
    )
    if isinstance(resolved_ref, GitHubRef):
        resolved_ref = replace(resolved_ref, rev=_resolve_github_commit(resolved_ref))
    source_text = fetch_agent_ref(
        resolved_ref,
        progress=progress,
        progress_id=progress_id,
    )
    parse_visiting_agent(
        layout,
        ref=_display_ref(resolved_ref.render()),
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
            declared_ref=target.declared_ref,
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
        raise ValueError(
            f"agent ref must point to a .too program: {_display_ref(text)}"
        )


def _fetch_http_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "toolang/0.1"})
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except (HTTPError, URLError) as exc:
        raise ValueError(f"could not fetch agent program: {_display_ref(url)}") from exc


def _visiting_program_cache_fresh(program_path: Path) -> bool:
    if not program_path.is_file():
        return False
    return time.time() - program_path.stat().st_mtime <= VISITING_PROGRAM_CACHE_TTL_SEC


def _resolve_github_commit(ref: GitHubRef) -> str:
    return pin_github_ref(ref, lambda url: _fetch_http_text(url).encode("utf-8")).rev


def _cached_visiting_source(layout: AgentLayout) -> AcquiredSource | None:
    try:
        receipt = json.loads((layout.home / "agent.source.json").read_bytes())
        if not isinstance(receipt, dict) or not all(
            isinstance(receipt.get(key), str) and receipt[key]
            for key in ("declared_ref", "resolved_ref", "entry", "sha256")
        ):
            return None
        content = layout.program.read_bytes()
        if receipt["sha256"] != sha256(content).hexdigest():
            return None
        entry = receipt["entry"]
        resolved = parse_remote_source(receipt["resolved_ref"])
        if (
            isinstance(resolved, GitHubShorthand)
            or resolved.render() != receipt["resolved_ref"]
            or resolved.path.lstrip("/") != entry
            or not reusable_agent_source(
                parse_agent_target(receipt["declared_ref"]), receipt["resolved_ref"]
            )
        ):
            return None
        return AcquiredSource(
            receipt["declared_ref"],
            Acquisition(
                receipt["resolved_ref"],
                entry,
                {entry: content},
                layout.name,
            ),
        )
    except (OSError, KeyError, TypeError, ValueError):
        return None


def restore_program_acquisition(layout: AgentLayout) -> AgentLayout:
    """Carry a verified visiting cache receipt across the workload boundary."""
    if layout.program_acquisition is not None or layout.managed_membership:
        return layout
    captured = _cached_visiting_source(layout)
    return replace(layout, program_acquisition=captured) if captured else layout


def reusable_agent_source(target: AgentTarget, resolved_ref: str) -> bool:
    """Check effective Mod source selection without polling an upstream version."""
    previous = parse_remote_source(resolved_ref)
    requested = target.source
    if isinstance(requested, GitHubShorthand):
        return (
            isinstance(previous, GitHubRef)
            and previous.owner == requested.owner
            and previous.repo == (requested.repo or "agents")
            and previous.default_name() == requested.name
        )
    if isinstance(requested, GitHubRef):
        return (
            isinstance(previous, GitHubRef)
            and (not requested.is_pinned or requested.rev == previous.rev)
            and (previous.owner, previous.repo, previous.path)
            == (requested.owner, requested.repo, requested.path)
        )
    return isinstance(requested, HttpRef) and requested == previous
