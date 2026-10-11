"""Immutable effective chore definitions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.common.ids import chore_id
from toolang.common.layout import AgentLayout
from toolang.lang.ast import ChoreDecl, Program


_REMOTE_REF_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


@dataclass(frozen=True, slots=True)
class Chore:
    """One current chore definition."""

    name: str
    title: str | None
    body: str
    schedule: str | None
    revision: str
    source: str
    path: Path | None

    @property
    def id(self) -> str:
        return chore_id(self.name)

    @property
    def ref(self) -> str:
        return f"chore/{self.name}"

    @property
    def thread_id(self) -> str:
        """Return the stable execution thread derived from chore identity."""

        return f"chore_{self.id}"


def load_chores(layout: AgentLayout) -> tuple[Chore, ...]:
    """Load normalized current Markdown chores for one agent home."""

    groups = [
        tuple(_file_chore(item) for item in AuthoredChores(directory).list())
        for directory in dict.fromkeys((layout.root, layout.home))
    ]
    return tuple(merge_chores(*groups).values())


def program_chores(program: Program) -> tuple[Chore, ...]:
    """Normalize program chore declarations."""

    return tuple(_program_chore(decl) for decl in program.chores)


def merge_chores(*groups: Iterable[Chore]) -> dict[str, Chore]:
    """Merge effective chore sources with later layers shadowing earlier layers by name."""

    merged: dict[str, Chore] = {}
    for group in groups:
        for chore in group:
            previous = merged.get(chore.id)
            if previous is not None and previous.name != chore.name:
                raise ValueError(
                    f"chore ID collision {chore.id!r}: {previous.source} and {chore.source}"
                )
            merged[chore.id] = chore
    return merged


def schedule_revision(chore: Chore) -> str | None:
    """Return the stable revision of one chore schedule."""

    if chore.schedule is None:
        return None
    return sha256(chore.schedule.encode()).hexdigest()


def _file_chore(chore: ChoreFile) -> Chore:
    if chore.path is None:
        raise ValueError("authored chore path is required")
    return _chore(
        name=chore.name,
        title=chore.title,
        body=chore.body,
        schedule=chore.schedule,
        source=str(chore.path),
        path=chore.path,
    )


def _program_chore(decl: ChoreDecl) -> Chore:
    title = str(decl.meta.get("title") or "").strip()
    return _chore(
        name=decl.name,
        title=title or None,
        body=decl.body.strip(),
        schedule=str(decl.meta["schedule"]).strip()
        if decl.meta.get("schedule")
        else None,
        source=f"agent.too:{decl.span.line}",
        path=None,
    )


def _chore(
    *,
    name: str,
    title: str | None,
    body: str,
    schedule: str | None,
    source: str,
    path: Path | None,
) -> Chore:
    normalized_body = body.replace("\r\n", "\n").strip()
    return Chore(
        name=name,
        title=title,
        body=body,
        schedule=schedule,
        revision=sha256(normalized_body.encode()).hexdigest(),
        source=source,
        path=path,
    )


def chore_thread_id(chore: ChoreFile | Chore) -> str:
    """Return the runtime thread projection for one authored chore."""

    return f"chore_{chore.id}"


def chore_display_title(chore: ChoreFile | Chore, *, fallback: str) -> str:
    """Return a short title for a caller-facing chore projection."""

    if chore.title:
        return chore.title
    for line in chore.body.splitlines():
        candidate = line.strip().lstrip("#").strip()
        if candidate:
            return candidate[:80]
    return fallback


def chore_remote_status(chore: ChoreFile | Chore) -> str | None:
    """Return the first explicit remote status in a chore body."""

    for line in chore.body.splitlines():
        key, separator, value = line.partition(":")
        if separator == ":" and key.strip().lower() in {"status", "remote status"}:
            return value.strip() or None
    return None


def chore_remote_ref(chore: ChoreFile | Chore) -> str | None:
    """Return a remote work-item key projected from authored chore content."""

    text = "\n".join(part for part in (chore.title or "", chore.body) if part)
    match = _REMOTE_REF_PATTERN.search(text)
    return None if match is None else match.group(0)
