"""Package-neutral GitHub reference parsing and rendering."""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Callable
import json
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


@dataclass(frozen=True, slots=True)
class GitHubRef:
    """One canonical GitHub source reference."""

    owner: str
    repo: str
    path: str
    rev: str

    def render(self) -> str:
        """Render this reference as one canonical ``github://`` URI."""

        return f"github://{self.owner}/{self.repo}/{self.path}@{self.rev}"

    def default_name(self) -> str:
        """Return the filename-derived default name for this reference."""

        return Path(self.path).stem


@dataclass(frozen=True, slots=True)
class GitHubShorthand:
    """GitHub source syntax; callers own repository and kind-specific probes."""

    owner: str
    name: str
    repo: str | None = None

    def render(self) -> str:
        path = f"{self.repo}/{self.name}" if self.repo is not None else self.name
        return f"gh:{self.owner}/{path}"


def parse_github_source_ref(text: str) -> GitHubRef | GitHubShorthand | None:
    """Parse explicit GitHub aliases and URLs without network access."""
    if text.startswith("github://"):
        return parse_github_ref(text)
    if text.startswith(("gh:", "github:")):
        payload = text.partition(":")[2]
        if "@" in payload:
            return parse_github_ref(f"github://{payload}")
        parts = payload.split("/")
        if len(parts) not in {2, 3} or any(
            not part or part in {".", ".."} or any(char in part for char in ":?#\\")
            for part in parts
        ):
            raise ValueError(f"invalid GitHub Source Ref: {text}")
        return GitHubShorthand(
            owner=parts[0], name=parts[-1], repo=parts[1] if len(parts) == 3 else None
        )
    return parse_github_url(text)


def parse_github_ref(text: str) -> GitHubRef:
    """Parse one canonical ``github://`` reference with an explicit revision."""

    parsed = urlsplit(text)
    if parsed.scheme != "github":
        raise ValueError(f"unsupported GitHub ref: {text}")
    owner = parsed.netloc.strip()
    repo, separator, target = parsed.path.strip("/").partition("/")
    if not owner or not separator or not repo or not target:
        raise ValueError(f"invalid GitHub ref: {text}")
    path, revision_separator, rev = target.rpartition("@")
    if not revision_separator:
        raise ValueError(f"GitHub ref must include @rev: {text}")
    if not path or not rev:
        raise ValueError(f"invalid GitHub ref: {text}")
    return GitHubRef(owner=owner, repo=repo, path=path, rev=rev)


def github_raw_url(ref: GitHubRef) -> str:
    """Return the raw-content URL for one GitHub reference."""

    owner = quote(ref.owner, safe="")
    repo = quote(ref.repo, safe="")
    rev = quote(ref.rev, safe="/")
    path = quote(ref.path.lstrip("/"), safe="/")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{rev}/{path}"


def parse_github_url(text: str) -> GitHubRef | None:
    """Parse a GitHub tree, blob, or raw-content URL when recognized."""

    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc == "github.com":
        parts = _url_path_parts(parsed.path)
        if len(parts) < 5 or parts[2] not in {"blob", "tree"}:
            raise ValueError(f"invalid GitHub URL: {text}")
        owner, repo = parts[:2]
        rev, path = _split_url_revision(parts[3:], original=text)
        return GitHubRef(owner=owner, repo=repo, path=path, rev=rev)
    if parsed.netloc == "raw.githubusercontent.com":
        parts = _url_path_parts(parsed.path)
        if len(parts) < 4:
            raise ValueError(f"invalid GitHub URL: {text}")
        owner, repo = parts[:2]
        rev, path = _split_url_revision(parts[2:], original=text)
        return GitHubRef(owner=owner, repo=repo, path=path, rev=rev)
    return None


def _url_path_parts(path: str) -> list[str]:
    return [unquote(part) for part in path.strip("/").split("/") if part]


def _split_url_revision(parts: list[str], *, original: str) -> tuple[str, str]:
    if len(parts) >= 4 and parts[0] == "refs" and parts[1] in {"heads", "tags"}:
        return "/".join(parts[:3]), "/".join(parts[3:])
    if len(parts) >= 2:
        return parts[0], "/".join(parts[1:])
    raise ValueError(f"invalid GitHub URL: {original}")


def pin_github_ref(ref: GitHubRef, fetch: Callable[[str], bytes]) -> GitHubRef:
    """Pin a selector using bytes acquired by its owning source service."""
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", ref.rev):
        return ref
    url = (
        f"https://api.github.com/repos/{quote(ref.owner, safe='')}/"
        f"{quote(ref.repo, safe='')}/commits/{quote(ref.rev, safe='')}"
    )
    data = json.loads(fetch(url))
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("sha"), str)
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", data["sha"])
    ):
        raise ValueError(f"invalid GitHub commit response: {ref.render()}")
    return replace(ref, rev=data["sha"])
