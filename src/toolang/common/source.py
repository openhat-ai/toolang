"""Explicit remote Source Ref syntax shared by Mod and Cap acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping
from types import MappingProxyType
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


@dataclass(frozen=True, slots=True)
class Acquisition:
    """Transport-independent captured source content with one entry file."""

    resolved_ref: str
    entry: str
    files: Mapping[str, bytes]
    name: str

    def __post_init__(self) -> None:
        if not self.resolved_ref or self.entry not in self.files:
            raise ValueError("acquisition requires a resolved ref and captured entry")
        for key in self.files:
            path = Path(key)
            if (
                not key
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in key
                or path.as_posix() != key
            ):
                raise ValueError("acquisition paths must be portable and relative")
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))

    @property
    def key(self) -> str:
        from hashlib import sha256

        return sha256(self.resolved_ref.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AcquiredSource:
    """A declaration paired with the exact content supplied by its source owner."""

    declared_ref: str
    acquisition: Acquisition

    def resolution(self) -> dict[str, str]:
        return {
            "declared_ref": self.declared_ref,
            "resolved_ref": self.acquisition.resolved_ref,
            "materialized": f"{self.acquisition.key}/{self.acquisition.entry}",
        }


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
