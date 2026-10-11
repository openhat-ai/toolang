"""Current chore definitions, independent of handlers and execution state."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.common.ids import chore_id
from toolang.common.layout import AgentLayout
from toolang.lang.ast import ChoreDecl, parse_chore_declarations


@dataclass(frozen=True, slots=True)
class Chore:
    """One effective definition and its exact authored location."""

    name: str
    title: str | None
    body: str
    schedule: str | None
    path: Path
    line: int | None = None

    @property
    def id(self) -> str:
        return chore_id(self.name)

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


def load_chore_files(layout: AgentLayout) -> tuple[Chore, ...]:
    """Read immediate root/home Markdown definitions, with home taking precedence."""

    return tuple(
        merge_chores(
            *(
                (file_chore(item) for item in AuthoredChores(directory).list())
                for directory in dict.fromkeys((layout.root, layout.home))
            )
        ).values()
    )


def load_chores(layout: AgentLayout) -> tuple[Chore, ...]:
    """Read effective authored definitions without preparing State or handlers."""

    source = (
        layout.program.read_text(encoding="utf-8") if layout.program.is_file() else ""
    )
    inline = inline_chores(parse_chore_declarations(source), path=layout.program)
    return tuple(merge_chores(load_chore_files(layout), inline).values())


def inline_chores(
    declarations: Iterable[ChoreDecl], *, path: Path
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
        )
        for decl in declarations
    )


def file_chore(chore: ChoreFile) -> Chore:
    if chore.path is None:
        raise ValueError("authored chore path is required")
    return Chore(
        name=chore.name,
        title=chore.title,
        body=chore.body,
        schedule=chore.schedule,
        path=chore.path,
    )


def merge_chores(*groups: Iterable[Chore]) -> dict[str, Chore]:
    """Later definitions shadow earlier definitions with the same name."""

    merged: dict[str, Chore] = {}
    for group in groups:
        for chore in group:
            previous = merged.get(chore.id)
            if previous is not None and previous.name != chore.name:
                raise ValueError(
                    f"chore ID collision {chore.id!r}: {previous.location} and {chore.location}"
                )
            merged[chore.id] = chore
    return merged
