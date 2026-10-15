"""Cap Source acquisition; transport and upstream version rules stay outside State."""

from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
import io
import json
from pathlib import Path
import tarfile
from threading import RLock
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from toolang.common.progress import ProgressSink, emit_progress
from toolang.common.github import (
    GitHubRef,
    GitHubShorthand,
    github_raw_url,
    parse_github_ref,
    pin_github_ref,
)
from toolang.common.source import Acquisition, parse_remote_source
from .types import CapKind as EntryKind, CAP_DIR_BY_KIND as DIR_NAME_BY_KIND


def resolve_remote_ref(
    kind: EntryKind,
    ref: str,
    *,
    progress: ProgressSink | None = None,
    progress_id: str | None = None,
) -> str:
    """Resolve one remote cap ref or shorthand to a canonical ref."""

    text = ref.strip()
    item_id = progress_id or _cap_progress_id(kind, text)
    emit_progress(
        progress,
        id=item_id,
        kind="prepare",
        stage="resolve",
        label=f"Resolving {kind}...",
        status="running",
        detail=text,
        subject="cap",
        ref=text,
    )
    try:
        parsed = parse_remote_source(text)
        if not isinstance(parsed, GitHubShorthand):
            canonical = canonicalize_remote_ref(kind, text)
            if canonical.startswith("github://") and not _github_remote_exists(
                kind, canonical
            ):
                raise ValueError(
                    f"remote {kind} not found or missing entry file: {ref}"
                )
        else:
            candidates = _remote_ref_candidates(kind, parsed)
            canonical = next(
                (
                    candidate
                    for candidate in candidates
                    if _github_remote_exists(kind, candidate)
                ),
                "",
            )
            if not canonical:
                raise ValueError(f"could not resolve remote {kind} shorthand: {ref}")
    except Exception as exc:
        emit_progress(
            progress,
            id=item_id,
            kind="prepare",
            stage="resolve",
            label=f"Failed to resolve {kind}",
            status="failed",
            detail=str(exc),
            subject="cap",
            ref=text,
            complete=True,
        )
        raise
    emit_progress(
        progress,
        id=item_id,
        kind="prepare",
        stage="resolve",
        label=f"Resolved {kind}",
        status="ok",
        detail=canonical,
        subject="cap",
        ref=text,
        complete=True,
    )
    return canonical


def canonicalize_remote_ref(kind: EntryKind, ref: str) -> str:
    """Canonicalize one explicit or shorthand remote cap ref."""

    source = parse_remote_source(ref)
    if isinstance(source, GitHubShorthand):
        return _remote_ref_candidates(kind, source)[0]
    if isinstance(source, GitHubRef):
        if kind == "skill" and Path(source.path).name == "SKILL.md":
            source = replace(source, path=Path(source.path).parent.as_posix())
        return source.render()
    url = urlparse(source.url)
    if kind == "skill" and Path(url.path).name == "SKILL.md":
        url = url._replace(path=url.path.removesuffix("/SKILL.md"))
    return url.geturl()


def remote_entry_name(kind: EntryKind, ref: str) -> str:
    """Return the default authored name for one remote cap ref."""

    canonical_ref = canonicalize_remote_ref(kind, ref)
    if canonical_ref.startswith("github://"):
        path = parse_github_ref(canonical_ref).path.rstrip("/")
        if not path:
            raise ValueError(f"invalid remote ref: {ref}")
        return Path(path).name if kind == "skill" else Path(path).stem
    parsed = urlparse(canonical_ref)
    path = parsed.path.rstrip("/")
    if not path:
        raise ValueError(f"invalid remote ref: {ref}")
    name = Path(path).name.split("@", 1)[0]
    return name if kind == "skill" else Path(name).stem


def _remote_ref_candidates(kind: EntryKind, source: GitHubShorthand) -> tuple[str, ...]:
    owner, name = source.owner, source.name
    if source.repo is not None:
        return _remote_ref_candidates_for_repo(kind, owner, source.repo, name)
    repositories: dict[EntryKind, tuple[tuple[str, str], ...]] = {
        "skill": (
            ("agents", f"skills/{name}"),
            ("agent-skills", name),
            ("agent-skills", f"skills/{name}"),
            ("skills", name),
            ("skills", f"skills/{name}"),
        ),
        "service": (
            ("agents", f"services/{name}.md"),
            ("agent-services", f"{name}.md"),
            ("services", f"{name}.md"),
        ),
        "prompt": (
            ("agents", f"prompts/{name}.md"),
            ("agent-prompts", f"{name}.md"),
            ("prompts", f"{name}.md"),
        ),
        "psyche": (
            ("agents", f"psyches/{name}.md"),
            ("agent-psyches", f"{name}.md"),
            ("psyches", f"{name}.md"),
        ),
    }
    return tuple(
        _github_remote_ref_with_default_branch(owner, repo, path)
        for repo, path in repositories[kind]
    )


def _remote_ref_candidates_for_repo(
    kind: EntryKind,
    owner: str,
    repo: str,
    name: str,
) -> tuple[str, ...]:
    return tuple(
        _github_remote_ref_with_default_branch(owner, repo, path)
        for path in _remote_path_candidates_for_repo(kind, repo, name)
    )


def _remote_path_candidates_for_repo(
    kind: EntryKind,
    repo: str,
    name: str,
) -> tuple[str, ...]:
    if kind == "skill":
        if repo in {"agent-skills", "skills"}:
            return (name, f"skills/{name}")
        return (f"skills/{name}", name)
    directory = DIR_NAME_BY_KIND[kind]
    if repo in {f"agent-{directory}", directory}:
        return (f"{name}.md",)
    return (f"{directory}/{name}.md", f"{name}.md")


def _github_remote_ref_with_default_branch(owner: str, repo: str, path: str) -> str:
    try:
        rev = _github_repo_default_branch(owner, repo)
    except ValueError:
        rev = "main"
    return GitHubRef(owner=owner, repo=repo, path=path, rev=rev).render()


def _github_remote_exists(kind: EntryKind, ref: str) -> bool:
    github_ref = parse_github_ref(ref)
    probe_ref = github_ref
    if kind == "skill":
        probe_ref = GitHubRef(
            owner=github_ref.owner,
            repo=github_ref.repo,
            path=(Path(github_ref.path) / "SKILL.md").as_posix(),
            rev=github_ref.rev,
        )
    request = Request(
        github_raw_url(probe_ref),
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


def _fetch_remote_cap_files(
    *,
    relative_entry_path: Path,
    kind: EntryKind,
    name: str,
    ref: str,
    progress: ProgressSink | None = None,
    progress_id: str | None = None,
) -> dict[str, bytes]:
    """Fetch remote cap bytes keyed by their relative prepared paths."""

    source = parse_remote_source(ref)
    if isinstance(source, GitHubShorthand):
        raise ValueError(
            f"remote {kind} source must be resolved before fetching: {ref}"
        )
    item_id = progress_id or _cap_progress_id(kind, ref)
    emit_progress(
        progress,
        id=item_id,
        kind="prepare",
        stage="fetch",
        label=f"Fetching {kind} {name}...",
        status="running",
        detail=ref,
    )
    if kind == "skill":
        try:
            if isinstance(source, GitHubRef):
                files = _fetch_github_directory(source)
            else:
                url = urlparse(source.url)
                entry_url = url._replace(
                    path=f"{url.path.rstrip('/')}/SKILL.md"
                ).geturl()
                files = {"SKILL.md": _fetch_url_bytes(entry_url)}
        except Exception as exc:
            emit_progress(
                progress,
                id=item_id,
                kind="prepare",
                stage="fetch",
                label=f"Failed to fetch {kind} {name}",
                status="failed",
                detail=str(exc),
            )
            raise
        if "SKILL.md" not in files:
            emit_progress(
                progress,
                id=item_id,
                kind="prepare",
                stage="fetch",
                label=f"Failed to fetch {kind} {name}",
                status="failed",
                detail=f"remote skill is missing SKILL.md: {ref}",
            )
            raise ValueError(f"remote skill is missing SKILL.md: {ref}")
        root = relative_entry_path.parent
        materialized = {
            (root / relative_path).as_posix(): content
            for relative_path, content in files.items()
        }
    else:
        try:
            materialized = {
                relative_entry_path.as_posix(): (
                    _fetch_github_file(source)
                    if isinstance(source, GitHubRef)
                    else _fetch_url_bytes(source.url)
                )
            }
        except Exception as exc:
            emit_progress(
                progress,
                id=item_id,
                kind="prepare",
                stage="fetch",
                label=f"Failed to fetch {kind} {name}",
                status="failed",
                detail=str(exc),
            )
            raise
    emit_progress(
        progress,
        id=item_id,
        kind="prepare",
        stage="fetch",
        label=f"Fetched {kind} {name}",
        status="ok",
        detail=f"{len(materialized)} {'file' if len(materialized) == 1 else 'files'}",
    )
    return materialized


def _cap_progress_id(kind: EntryKind, ref: str) -> str:
    return f"cap:{kind}:{ref.strip()}"


def _fetch_github_directory(ref: GitHubRef) -> dict[str, bytes]:
    root = ref.path.strip("/")
    prefix = f"{root}/"
    archive_url = (
        f"https://codeload.github.com/{quote(ref.owner, safe='')}/"
        f"{quote(ref.repo, safe='')}/tar.gz/{quote(ref.rev, safe='')}"
    )
    archive_bytes = _fetch_url_bytes(archive_url)
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            member_path = Path(member.name)
            path = "/".join(member_path.parts[1:])
            if not path.startswith(prefix):
                continue
            relative_path = path.removeprefix(prefix)
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            files[relative_path] = extracted.read()
    if not files:
        raise ValueError(f"could not fetch remote directory: {ref.render()}")
    return files


def _fetch_github_file(ref: GitHubRef) -> bytes:
    return _fetch_url_bytes(github_raw_url(ref))


def _fetch_url_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "toolang/0.1"})
    try:
        with urlopen(request, timeout=30) as response:
            return response.read()
    except (HTTPError, URLError) as exc:
        raise ValueError(f"could not fetch remote content: {url}") from exc


_pinned: ContextVar[tuple[dict[str, str], RLock] | None] = ContextVar(
    "source_pins", default=None
)
_refresh: ContextVar[bool] = ContextVar("source_refresh", default=False)


@contextmanager
def acquisition_session(*, refresh: bool = False) -> Iterator[None]:
    """Keep upstream selection consistent across one preparation attempt."""
    token = _pinned.set(({}, RLock()))
    refresh_token = _refresh.set(refresh)
    try:
        yield
    finally:
        _refresh.reset(refresh_token)
        _pinned.reset(token)


def reusable(kind: EntryKind, declared_ref: str, resolved_ref: str) -> bool:
    """Check current source selection without checking mutable upstream versions."""
    requested = parse_remote_source(declared_ref)
    previous = parse_remote_source(resolved_ref)
    if isinstance(requested, GitHubShorthand):
        return (
            isinstance(previous, GitHubRef)
            and requested.owner == previous.owner
            and (requested.repo is None or requested.repo == previous.repo)
            and remote_entry_name(kind, resolved_ref) == requested.name
        )
    if isinstance(requested, GitHubRef):
        return (
            isinstance(previous, GitHubRef)
            and requested.owner == previous.owner
            and requested.repo == previous.repo
            and (not requested.is_pinned or requested.rev == previous.rev)
            and canonicalize_remote_ref(kind, declared_ref).rsplit("@", 1)[0]
            == canonicalize_remote_ref(kind, resolved_ref).rsplit("@", 1)[0]
        )
    return canonicalize_remote_ref(kind, declared_ref) == canonicalize_remote_ref(
        kind, resolved_ref
    )


def _resolve_github_commit(ref: GitHubRef) -> str:
    return pin_github_ref(ref, _fetch_url_bytes).rev


def _pin(ref: GitHubRef) -> GitHubRef:
    session = _pinned.get()
    if session is None:
        return replace(ref, rev=_resolve_github_commit(ref))
    values, lock = session
    key = f"{ref.owner}/{ref.repo}@{ref.rev}"
    with lock:
        if key not in values:
            values[key] = _resolve_github_commit(ref)
        return replace(ref, rev=values[key])


def acquire(
    kind: EntryKind,
    declared_ref: str,
    *,
    cached: Acquisition | None = None,
    refresh: bool | None = None,
    progress: ProgressSink | None = None,
    progress_id: str | None = None,
) -> Acquisition:
    """Resolve and capture a Cap using the currently selected Source owner."""
    if refresh is None:
        refresh = _refresh.get()
    if (
        cached is not None
        and not refresh
        and reusable(kind, declared_ref, cached.resolved_ref)
    ):
        return cached
    parsed = parse_remote_source(declared_ref)
    canonical = (
        resolve_remote_ref(
            kind, declared_ref, progress=progress, progress_id=progress_id
        )
        if isinstance(parsed, GitHubShorthand)
        else canonicalize_remote_ref(kind, declared_ref)
    )
    source = parse_remote_source(canonical)
    if isinstance(source, GitHubShorthand):
        raise ValueError("acquisition requires a canonical Source Ref")
    if isinstance(source, GitHubRef):
        source = _pin(source)
        canonical = source.render()
        if cached is not None and cached.resolved_ref == canonical:
            return cached
        entry = Path(source.path) / "SKILL.md" if kind == "skill" else Path(source.path)
    else:
        entry = Path(
            "SKILL.md" if kind == "skill" else Path(source.path).name or "content"
        )
    name = remote_entry_name(kind, canonical)
    if progress is None:
        files = _fetch_remote_cap_files(
            relative_entry_path=entry,
            kind=kind,
            name=name,
            ref=canonical,
        )
    else:
        files = _fetch_remote_cap_files(
            relative_entry_path=entry,
            kind=kind,
            name=name,
            ref=canonical,
            progress=progress,
            progress_id=progress_id,
        )
    return Acquisition(canonical, entry.as_posix(), files, name)


def source_location(ref: str, *, shape: str) -> str:
    """Render a human-facing source link without changing its captured identity."""
    if not ref.startswith("github://"):
        return ref
    try:
        source = parse_github_ref(ref)
    except ValueError:
        return ref
    view = "blob" if shape == "file" else "tree"
    return (
        f"https://github.com/{quote(source.owner, safe='')}/"
        f"{quote(source.repo, safe='')}/{view}/{quote(source.rev, safe='/')}/"
        f"{quote(source.path, safe='/')}"
    )
