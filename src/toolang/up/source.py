"""Resolve and materialize local or remote agent Mod sources."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import ssl
import time
from urllib.parse import quote, urlsplit
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
    import httpx

    from .addresses import AgentAddress

AgentTargetForm = Literal["name", "shorthand", "ref"]


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
    from toolang.common.config_sources import script_sources, source_catalog

    for directory in (
        layout.agent_root,
        layout.agent_home.parent,
        layout.agent_home,
        layout.root_config.parent,
    ):
        if directory.is_symlink():
            raise ValueError(
                f"shared agent directory must not be a symlink: {directory}"
            )
        directory.mkdir(parents=True, exist_ok=True)
    with EditLock(layout.place_state / "edit.lock").write():
        candidates = script_sources(resolved_source)
        selected_config = next(
            (item.path for item in reversed(candidates) if item.path.is_file()), None
        )
        catalog = source_catalog(candidates, roaming=True, validate=False)
        previous = (
            json.loads(layout.source_receipt.read_bytes())
            if layout.source_receipt.is_file()
            else {}
        )
        if previous.get("source_path", str(resolved_source)) != str(resolved_source):
            raise FileExistsError(
                f"agent home {layout.agent_home} already belongs to {previous['source_path']}"
            )
        if (
            layout.program.exists()
            and not previous
            and not layout.program.samefile(resolved_source)
        ):
            raise FileExistsError(f"cannot replace authored file: {layout.program}")
        origins = {"program": str(resolved_source)}
        previous_root = (
            json.loads(layout.root_source_receipt.read_bytes())
            if layout.root_source_receipt.is_file()
            else {}
        )
        previous_origins = {
            **previous.get("origins", {}),
            **previous_root.get("origins", {}),
        }
        for target, source, key in (
            (layout.program, resolved_source, "program"),
            (layout.root_config, selected_config, "root"),
            (layout.root_catalog, catalog, "root/catalog.json"),
        ):
            exists = target.exists() or target.is_symlink()
            if (
                exists
                and key not in previous_origins
                and not (
                    source is not None
                    and source.is_file()
                    and target.is_file()
                    and target.samefile(source)
                )
            ):
                raise ValueError(
                    f"fetched source conflicts with existing entry: {target}"
                )
            if source is not None:
                if key == "root/catalog.json" and not source.exists():
                    target.unlink(missing_ok=True)
                else:
                    _hardlink_source(source, target)
                origins[key] = str(source)
            elif key in previous_origins:
                target.unlink(missing_ok=True)
        for path, value in (
            (
                layout.root_source_receipt,
                {
                    "origins": {
                        key: value for key, value in origins.items() if key != "program"
                    }
                },
            ),
            (
                layout.source_receipt,
                {
                    "source_path": str(resolved_source),
                    "sha256": sha256(layout.program.read_bytes()).hexdigest(),
                    "origins": {"program": str(resolved_source)},
                },
            ),
        ):
            receipt = json.dumps(value, sort_keys=True)
            if not path.is_file() or path.read_text() != receipt:
                atomic_write_text(path, receipt)
    return layout


def _hardlink_source(source: Path, target: Path) -> None:
    """Publish an ordinary source entry without copying bytes or permissions."""
    if not source.is_file():
        raise ValueError(f"source must be a file: {source}")
    if target.is_file() and not target.is_symlink() and target.samefile(source):
        return
    import tempfile

    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(descriptor)
    staged = Path(name)
    staged.unlink()
    try:
        os.link(source.resolve(), staged)
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


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
            layout.source_receipt,
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
    from toolang.state.source import parse_agent_source

    captured = _cached_visiting_source(layout)
    if (
        captured is not None
        and reusable_agent_source(target, captured.acquisition.resolved_ref)
        and _visiting_program_cache_fresh(layout.program)
    ):
        layout = replace(layout, program_acquisition=captured)
        parse_agent_source(
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
    parse_agent_source(
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
    import httpx

    url = github_raw_url(ref)
    try:
        response = httpx.head(
            url,
            headers={"User-Agent": "toolang/0.1"},
            timeout=30,
            follow_redirects=True,
        )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        raise ValueError(
            f"could not check GitHub agent source: {_display_ref(url)} "
            f"({_http_error_reason(exc)})"
        ) from exc


def _github_repo_default_branch(owner: str, repo: str) -> str:
    import httpx

    api_url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    )
    try:
        response = httpx.get(
            api_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "toolang/0.1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
            follow_redirects=True,
        )
        response.raise_for_status()
        data = json.loads(response.content.decode("utf-8"))
    except httpx.HTTPError as exc:
        raise ValueError(
            f"could not resolve GitHub default branch: {owner}/{repo} "
            f"({_http_error_reason(exc)})"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"could not resolve GitHub default branch: {owner}/{repo} "
            "(invalid JSON response)"
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
    import httpx

    try:
        response = httpx.get(
            url,
            headers={"User-Agent": "toolang/0.1"},
            timeout=30,
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.content.decode("utf-8")
    except httpx.HTTPError as exc:
        raise ValueError(
            f"could not fetch agent program: {_display_ref(url)} "
            f"({_http_error_reason(exc)})"
        ) from exc


def _http_error_reason(exc: httpx.HTTPError) -> str:
    """Expose actionable categories without leaking URLs from transport errors."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "request timed out"
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ssl.SSLCertVerificationError):
            return "TLS certificate verification failed"
        cause = cause.__cause__ or cause.__context__
    return "network request failed"


def _visiting_program_cache_fresh(program_path: Path) -> bool:
    if not program_path.is_file():
        return False
    return time.time() - program_path.stat().st_mtime <= VISITING_PROGRAM_CACHE_TTL_SEC


def _resolve_github_commit(ref: GitHubRef) -> str:
    return pin_github_ref(ref, lambda url: _fetch_http_text(url).encode("utf-8")).rev


def _cached_visiting_source(layout: AgentLayout) -> AcquiredSource | None:
    try:
        receipt = json.loads(layout.source_receipt.read_bytes())
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
