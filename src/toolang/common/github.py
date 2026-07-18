"""Package-neutral GitHub reference parsing and rendering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit


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


def parse_github_url(text: str) -> GitHubRef | None:
    """Parse a GitHub tree, blob, or raw-content URL when recognized."""

    return _parse_github_url(text, web_views=frozenset({"blob", "tree"}))


def parse_github_file_url(text: str) -> GitHubRef | None:
    """Parse a GitHub blob or raw-content URL when recognized."""

    return _parse_github_url(text, web_views=frozenset({"blob"}))


def github_raw_url(ref: GitHubRef) -> str:
    """Return the raw-content URL for one GitHub reference."""

    owner = quote(ref.owner, safe="")
    repo = quote(ref.repo, safe="")
    rev = quote(ref.rev, safe="/")
    path = quote(ref.path.lstrip("/"), safe="/")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{rev}/{path}"


def _parse_github_url(text: str, *, web_views: frozenset[str]) -> GitHubRef | None:
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc == "github.com":
        parts = _url_path_parts(parsed.path)
        if len(parts) < 5 or parts[2] not in web_views:
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
    return [part for part in path.strip("/").split("/") if part]


def _split_url_revision(parts: list[str], *, original: str) -> tuple[str, str]:
    if len(parts) >= 4 and parts[0] == "refs" and parts[1] in {"heads", "tags"}:
        return "/".join(parts[:3]), "/".join(parts[3:])
    if len(parts) >= 2:
        return parts[0], "/".join(parts[1:])
    raise ValueError(f"invalid GitHub URL: {original}")
