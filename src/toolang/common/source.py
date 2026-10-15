"""Explicit remote Source Ref syntax shared by Mod and Cap acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .github import GitHubRef, GitHubShorthand, parse_github_source_ref


@dataclass(frozen=True, slots=True)
class HttpRef:
    url: str

    @property
    def path(self) -> str:
        return unquote(urlsplit(self.url).path)

    def render(self) -> str:
        return self.url

    def default_name(self) -> str:
        return Path(self.path).stem


RemoteSource = GitHubRef | GitHubShorthand | HttpRef


def parse_remote_source(ref: str) -> RemoteSource:
    """Reject unsupported sources before probing or fetching content."""
    text = ref.strip()
    github = parse_github_source_ref(text)
    if github is not None:
        return github
    url = urlsplit(text)
    if url.scheme in {"http", "https"} and url.netloc:
        return HttpRef(text)
    raise ValueError(
        f"unsupported Source Ref {ref!r}: use gh:, github:, github://, or a complete "
        "HTTP(S) URL; unprefixed remote refs and OpenHat sources are not supported"
    )
