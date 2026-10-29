"""Current chore definitions, independent of handlers and execution state."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import cast

from .chore import AuthoredChores, ChoreFile
from toolang.common.files import EditLock
from toolang.common.layout import AgentLayout
from toolang.lang.ast import ChoreDecl, parse_chore_declarations
from toolang.lang.errors import ToolangSourceError
from .chore_ids import chore_id, validate_chore_name


@dataclass(frozen=True, slots=True)
class Chore:
    """One effective definition and its exact authored location."""

    name: str
    title: str | None
    body: str
    schedule: str | None
    path: Path
    line: int | None = None
    source_error: str | None = None
    agent_name: str = field(kw_only=True)

    @property
    def id(self) -> str:
        return chore_id(self.name, agent_name=self.agent_name)

    @property
    def ref(self) -> str:
        return f"chore/{self.name}"

    @property
    def thread_id(self) -> str:
        return f"chore_{self.id}"

    @property
    def body_hash(self) -> str:
        return sha256(self.body.replace("\r\n", "\n").strip().encode()).hexdigest()

    @property
    def schedule_hash(self) -> str | None:
        return sha256(self.schedule.encode()).hexdigest() if self.schedule else None

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}" if self.line is not None else str(self.path)

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        for line in self.body.splitlines():
            candidate = line.strip().lstrip("#").strip()
            if candidate:
                return candidate[:80]
        return self.name

    def to_data(self) -> dict[str, object]:
        """Serialize accepted Markdown terms with a portable source identity."""
        if self.line is not None or self.source_error is not None:
            raise ValueError("only accepted Markdown chores have standalone terms")
        data: dict[str, object] = {
            "name": self.name,
            "title": self.title,
            "body": self.body,
            "schedule": self.schedule,
            "path": self.path.as_posix(),
        }
        self.from_data(data, agent_name=self.agent_name)
        return data

    @classmethod
    def from_data(cls, data: Mapping[str, object], *, agent_name: str) -> Chore:
        """Restore accepted terms without parsing or revalidating authored syntax."""
        if set(data) != {"name", "title", "body", "schedule", "path"}:
            raise ValueError("chore term fields do not match the schema")
        name, body, path = (data[key] for key in ("name", "body", "path"))
        if (
            not isinstance(name, str)
            or not isinstance(body, str)
            or not isinstance(path, str)
        ):
            raise TypeError("chore name, body, and path must be strings")
        validate_chore_name(name)
        if "\\" in path or path != (Path("chores") / f"{name}.md").as_posix():
            raise ValueError("chore term path must match its home Markdown name")
        title, schedule = (data[key] for key in ("title", "schedule"))
        for value in (title, schedule):
            if value is not None and (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ValueError(
                    "chore title and schedule must be normalized text or null"
                )
        return cls(
            name,
            cast(str | None, title),
            body,
            cast(str | None, schedule),
            Path(path),
            agent_name=agent_name,
        )


def load_chore_files(layout: AgentLayout) -> tuple[Chore, ...]:
    """Read immediate home Markdown definitions."""

    return tuple(
        file_chore(item, agent_name=layout.name)
        for item in AuthoredChores(
            layout.agent_home, agent_name=layout.name, lock_path=layout.home_edit_lock
        ).list()
    )


def load_chores(layout: AgentLayout) -> tuple[Chore, ...]:
    """Read effective authored definitions without preparing State or handlers."""

    return EditLock(layout.home_edit_lock).read(lambda: _load_chores(layout))


def _load_chores(layout: AgentLayout) -> tuple[Chore, ...]:
    inline: tuple[Chore, ...] = ()
    if layout.program.is_file():
        try:
            inline = inline_chores(
                parse_chore_declarations(layout.program.read_text(encoding="utf-8")),
                path=layout.program,
                agent_name=layout.name,
            )
        except (OSError, ValueError, ToolangSourceError):
            pass
    return tuple(merge_chores(load_chore_files(layout), inline).values())


def inspect_chore_sources(layout: AgentLayout) -> tuple[Chore, ...]:
    """Include malformed authored files for inspection and raw repair."""
    return EditLock(layout.home_edit_lock).read(lambda: _inspect_chore_sources(layout))


def _inspect_chore_sources(layout: AgentLayout) -> tuple[Chore, ...]:
    valid = load_chores(layout)
    accepted_names = {chore.name for chore in valid}
    invalid = tuple(
        Chore(
            path.stem,
            None,
            content,
            None,
            path,
            source_error=error,
            agent_name=layout.name,
        )
        for path, content, error in AuthoredChores(
            layout.agent_home, agent_name=layout.name, lock_path=layout.home_edit_lock
        ).sources()
        if error is not None and path.stem not in accepted_names
    )
    return (*invalid, *valid)


def inline_chores(
    declarations: Iterable[ChoreDecl], *, path: Path, agent_name: str
) -> tuple[Chore, ...]:
    return tuple(
        Chore(
            name=decl.name,
            title=str(decl.meta.get("title") or "").strip() or None,
            body=decl.body.strip(),
            schedule=str(decl.meta["schedule"]).strip()
            if decl.meta.get("schedule")
            else None,
            path=path,
            line=decl.span.line,
            agent_name=agent_name,
        )
        for decl in declarations
    )


def file_chore(chore: ChoreFile, *, agent_name: str) -> Chore:
    if chore.path is None:
        raise ValueError("authored chore path is required")
    return Chore(
        name=chore.name,
        title=chore.title,
        body=chore.body,
        schedule=chore.schedule,
        path=chore.path,
        agent_name=agent_name,
    )


def merge_chores(*groups: Iterable[Chore]) -> dict[str, Chore]:
    """Later definitions shadow earlier definitions with the same name."""

    merged: dict[str, Chore] = {}
    for group in groups:
        for chore in group:
            previous = merged.get(chore.id)
            if previous is not None and (previous.name, previous.agent_name) != (
                chore.name,
                chore.agent_name,
            ):
                raise ValueError(
                    f"chore ID collision {chore.id!r}: {previous.location} and {chore.location}"
                )
            merged[chore.id] = chore
    return merged
