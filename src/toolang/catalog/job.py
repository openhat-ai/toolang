"""Authored task and chore catalog."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, overload

from . import job_files as job_definitions


class JobCatalog:
    """CRUD over authored task and chore files for one agent."""

    def __init__(self, root: Path, name: str) -> None:
        self.root = root
        self.name = name

    @overload
    def list(
        self,
        *,
        kind: Literal["task"],
        lifecycle: job_definitions.JobLifecycle = "ready",
    ) -> tuple[job_definitions.TaskEntry, ...]: ...

    @overload
    def list(
        self,
        *,
        kind: Literal["chore"],
        lifecycle: job_definitions.JobLifecycle = "ready",
    ) -> tuple[job_definitions.ChoreEntry, ...]: ...

    @overload
    def list(
        self,
        *,
        kind: None = None,
        lifecycle: job_definitions.JobLifecycle = "ready",
    ) -> tuple[job_definitions.TaskEntry | job_definitions.ChoreEntry, ...]: ...

    @overload
    def list(
        self,
        *,
        kind: job_definitions.JobKind,
        lifecycle: job_definitions.JobLifecycle = "ready",
    ) -> tuple[job_definitions.TaskEntry | job_definitions.ChoreEntry, ...]: ...

    def list(
        self,
        *,
        kind: job_definitions.JobKind | None = None,
        lifecycle: job_definitions.JobLifecycle = "ready",
    ) -> tuple[job_definitions.TaskEntry | job_definitions.ChoreEntry, ...]:
        tasks = (
            job_definitions._list_tasks(self.root, self.name, lifecycle=lifecycle)
            if kind in {None, "task"}
            else ()
        )
        chores = (
            job_definitions._list_chores(self.root, self.name, lifecycle=lifecycle)
            if kind in {None, "chore"}
            else ()
        )
        return (*tasks, *chores)

    @overload
    def get(
        self,
        kind: Literal["task"],
        job_id: str,
        *,
        lifecycle: job_definitions.JobLifecycle | None = "ready",
    ) -> job_definitions.TaskEntry | None: ...

    @overload
    def get(
        self,
        kind: Literal["chore"],
        job_id: str,
        *,
        lifecycle: job_definitions.JobLifecycle | None = "ready",
    ) -> job_definitions.ChoreEntry | None: ...

    @overload
    def get(
        self,
        kind: job_definitions.JobKind,
        job_id: str,
        *,
        lifecycle: job_definitions.JobLifecycle | None = "ready",
    ) -> job_definitions.TaskEntry | job_definitions.ChoreEntry | None: ...

    def get(
        self,
        kind: job_definitions.JobKind,
        job_id: str,
        *,
        lifecycle: job_definitions.JobLifecycle | None = "ready",
    ) -> job_definitions.TaskEntry | job_definitions.ChoreEntry | None:
        return job_definitions._find_job(
            self.root,
            self.name,
            kind=kind,
            job_id=job_id,
            lifecycle=lifecycle,
        )

    def create(
        self,
        kind: job_definitions.JobKind,
        text: str,
        *,
        lifecycle: job_definitions.JobLifecycle = "ready",
    ) -> Path:
        create = (
            job_definitions._create_task_text
            if kind == "task"
            else job_definitions._create_chore_text
        )
        return create(self.root, self.name, text, lifecycle=lifecycle)

    def update(self, kind: job_definitions.JobKind, job_id: str, text: str) -> Path:
        update = (
            job_definitions._update_task_text
            if kind == "task"
            else job_definitions._update_chore_text
        )
        return update(self.root, self.name, job_id, text)

    def read(self, kind: job_definitions.JobKind, job_id: str) -> str:
        load = (
            job_definitions._load_task_text
            if kind == "task"
            else job_definitions._load_chore_text
        )
        return load(self.root, self.name, job_id)

    def save(
        self,
        entry: job_definitions.TaskEntry | job_definitions.ChoreEntry,
        document: job_definitions.TaskFile | job_definitions.ChoreFile,
    ) -> Path:
        if isinstance(entry, job_definitions.TaskEntry) and isinstance(
            document, job_definitions.TaskFile
        ):
            return job_definitions._save_task_entry(
                self.root, self.name, entry, document
            )
        if isinstance(entry, job_definitions.ChoreEntry) and isinstance(
            document, job_definitions.ChoreFile
        ):
            return job_definitions._save_chore_entry(
                self.root, self.name, entry, document
            )
        raise TypeError("job entry and document kinds do not match")

    def create_document(
        self, document: job_definitions.TaskFile | job_definitions.ChoreFile
    ) -> Path:
        if isinstance(document, job_definitions.TaskFile):
            path = job_definitions.task_path(self.root, self.name, document.task_id())
        else:
            path = job_definitions.chore_path(self.root, self.name, document.chore_id())
        document.save(path)
        return path

    def clone(self, kind: job_definitions.JobKind, job_id: str) -> Path:
        clone = (
            job_definitions._clone_task
            if kind == "task"
            else job_definitions._clone_chore
        )
        return clone(self.root, self.name, job_id)

    def draft(self, kind: job_definitions.JobKind, job_id: str) -> Path | None:
        move = (
            job_definitions._draft_task
            if kind == "task"
            else job_definitions._draft_chore
        )
        return move(self.root, self.name, job_id)

    def ready(self, kind: job_definitions.JobKind, job_id: str) -> Path | None:
        move = (
            job_definitions._ready_task
            if kind == "task"
            else job_definitions._ready_chore
        )
        return move(self.root, self.name, job_id)

    def archive(self, kind: job_definitions.JobKind, job_id: str) -> Path | None:
        move = (
            job_definitions._archive_task
            if kind == "task"
            else job_definitions._archive_chore
        )
        return move(self.root, self.name, job_id)

    def reopen(self, kind: job_definitions.JobKind, job_id: str) -> Path | None:
        return self.ready(kind, job_id)

    def remove(self, kind: job_definitions.JobKind, job_id: str) -> bool:
        remove = (
            job_definitions._remove_archived_task
            if kind == "task"
            else job_definitions._remove_archived_chore
        )
        return remove(self.root, self.name, job_id)

    def allocate_id(self) -> str:
        return job_definitions.allocate_job_id(self.root, self.name)
