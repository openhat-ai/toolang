"""Local task and chore definition and lifecycle helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Literal

from dateutil.rrule import rrulestr
import frontmatter
from pydantic import BaseModel, ConfigDict, field_validator

from .common.ids import LOCAL_ID_FAMILY, allocate_id

JobKind = Literal["task", "chore"]
JobLifecycle = Literal["ready", "draft", "archived"]
TaskStatus = Literal["todo", "running", "done", "failed", "canceled"]
ChoreStatus = Literal["todo", "running", "done"]
DEFAULT_CHORE_SCHEDULE = "FREQ=HOURLY;INTERVAL=1"
REMOTE_REF_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


class _MarkdownDocument(BaseModel):
    """Base model for one authored markdown work document."""

    model_config = ConfigDict(extra="ignore")

    body: str = ""

    @classmethod
    def _load_markdown(cls, path: Path) -> "_MarkdownDocument":
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
        return cls.model_validate({**dict(post.metadata), "body": post.content})

    @classmethod
    def _parse_markdown(cls, text: str) -> "_MarkdownDocument":
        post = frontmatter.loads(text)
        return cls.model_validate({**dict(post.metadata), "body": post.content})

    def _save_markdown(self, path: Path) -> None:
        post = frontmatter.Post(self.body, None, **self.markdown_metadata())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(frontmatter.dumps(post), encoding="utf-8")

    def markdown_metadata(self) -> dict[str, object]:
        """Return persisted frontmatter metadata."""

        return {}

    def content_hash(self) -> str:
        """Return one stable textual signature for this definition."""

        payload = json.dumps(
            {"metadata": self.markdown_metadata(), "body": self.body},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TaskFile(_MarkdownDocument):
    """One local task definition."""

    id: str | None = None
    title: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _normalize_id(cls, value: object) -> object:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        id_factory: Callable[[], str] | None = None,
        persist_id: bool = False,
    ) -> "TaskFile":
        loaded = cls._load_markdown(path)
        document = cls.model_validate(loaded.model_dump(mode="python"))
        original = document
        if document.id is None and id_factory is not None:
            document = document.model_copy(update={"id": id_factory()})
        if document.id is None and persist_id:
            raise ValueError(f"task is missing id: {path}")
        if persist_id and document != original:
            document.save(path)
        return document

    @classmethod
    def parse_text(cls, text: str) -> "TaskFile":
        loaded = cls._parse_markdown(text)
        return cls.model_validate(loaded.model_dump(mode="python"))

    def with_id(self, id_factory: Callable[[], str]) -> "TaskFile":
        """Return this task with a stable id."""

        if self.id is not None:
            return self
        return self.model_copy(update={"id": id_factory()})

    def save(self, path: Path) -> None:
        if self.id is None:
            raise ValueError("task id is required")
        self._save_markdown(path)

    def task_id(self) -> str:
        if self.id is None:
            raise ValueError("task id is required")
        return self.id

    def thread_id(self) -> str:
        return task_thread_id(self.task_id())

    def remote_status(self) -> str | None:
        """Return the first explicit remote status line from the task body."""

        for line in self.body.splitlines():
            key, separator, value = line.partition(":")
            if separator != ":":
                continue
            normalized_key = key.strip().lower()
            if normalized_key in {"status", "remote status"}:
                text = value.strip()
                return text or None
        return None

    def remote_ref(self) -> str | None:
        """Return a stable remote work-item key when one is present."""

        text = "\n".join(part for part in (self.title or "", self.body) if part)
        match = REMOTE_REF_PATTERN.search(text)
        if match is None:
            return None
        return match.group(0)

    def render_input(self, *, fallback_name: str) -> str:
        """Return the prompt input for this task."""

        body = self.body.strip()
        if body:
            return body
        return fallback_name.strip()

    def display_title(self, *, fallback_name: str) -> str:
        """Return a short title for UI projection."""

        return _display_title(self.title, self.body, fallback=fallback_name)

    def markdown_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {}
        if self.id is not None:
            metadata["id"] = self.id
        if self.title is not None and self.title.strip():
            metadata["title"] = self.title.strip()
        return metadata


class ChoreFile(_MarkdownDocument):
    """One local chore definition."""

    id: str | None = None
    title: str | None = None
    schedule: str = DEFAULT_CHORE_SCHEDULE

    @field_validator("id", mode="before")
    @classmethod
    def _normalize_id(cls, value: object) -> object:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("schedule", mode="before")
    @classmethod
    def _normalize_schedule(cls, value: object) -> str:
        text = str(value or "").strip() or DEFAULT_CHORE_SCHEDULE
        rrulestr(text, dtstart=datetime(2026, 1, 1, tzinfo=timezone.utc))
        return text

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        id_factory: Callable[[], str] | None = None,
        persist_id: bool = False,
    ) -> "ChoreFile":
        loaded = cls._load_markdown(path)
        document = cls.model_validate(loaded.model_dump(mode="python"))
        original = document
        if document.id is None and id_factory is not None:
            document = document.model_copy(update={"id": id_factory()})
        if document.id is None and persist_id:
            raise ValueError(f"chore is missing id: {path}")
        if persist_id and document != original:
            document.save(path)
        return document

    @classmethod
    def parse_text(cls, text: str) -> "ChoreFile":
        loaded = cls._parse_markdown(text)
        return cls.model_validate(loaded.model_dump(mode="python"))

    def with_id(self, id_factory: Callable[[], str]) -> "ChoreFile":
        """Return this chore with a stable id."""

        if self.id is not None:
            return self
        return self.model_copy(update={"id": id_factory()})

    def save(self, path: Path) -> None:
        if self.id is None:
            raise ValueError("chore id is required")
        self._save_markdown(path)

    def chore_id(self) -> str:
        if self.id is None:
            raise ValueError("chore id is required")
        return self.id

    def thread_id(self) -> str:
        return chore_thread_id(self.chore_id())

    def render_input(self, *, fallback_title: str) -> str:
        """Return the prompt input for this chore."""

        title = (self.title or "").strip() or fallback_title.strip()
        body = self.body.strip()
        if title and body:
            return f"# {title}\n\n{body}"
        if body:
            return body
        return title

    def display_title(self, *, fallback_name: str) -> str:
        """Return a short title for UI projection."""

        return _display_title(self.title, self.body, fallback=fallback_name)

    def markdown_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {}
        if self.id is not None:
            metadata["id"] = self.id
        if self.title is not None and self.title.strip():
            metadata["title"] = self.title.strip()
        metadata["schedule"] = self.schedule
        return metadata


@dataclass(frozen=True, slots=True)
class TaskEntry:
    """One listed task definition."""

    name: str
    path: Path
    document: TaskFile
    lifecycle: JobLifecycle = "ready"


@dataclass(frozen=True, slots=True)
class ChoreEntry:
    """One listed chore definition."""

    name: str
    path: Path
    document: ChoreFile
    lifecycle: JobLifecycle = "ready"


def next_scheduled_at(
    schedule_text: str,
    *,
    anchor: datetime,
    not_before: datetime,
    inclusive: bool,
) -> datetime | None:
    """Return the next UTC occurrence for one RRULE."""

    anchor_utc = _as_utc(anchor)
    floor_utc = _as_utc(not_before)
    schedule = rrulestr(schedule_text, dtstart=anchor_utc)
    candidate = schedule.after(floor_utc, inc=inclusive)
    if candidate is None:
        return None
    return _as_utc(candidate)


def task_thread_id(task_id: str) -> str:
    """Return the normalized thread id for one task."""

    return f"tsk_{task_id.strip()}"


def chore_thread_id(chore_id: str) -> str:
    """Return the normalized thread id for one chore."""

    return f"chr_{chore_id.strip()}"


def task_id_from_thread_id(thread_id: str) -> str | None:
    """Extract one local task id from its canonical thread id."""

    if not thread_id.startswith("tsk_"):
        return None
    task_id = thread_id.removeprefix("tsk_").strip()
    return task_id or None


def chore_id_from_thread_id(thread_id: str) -> str | None:
    """Extract one local chore id from its canonical thread id."""

    if not thread_id.startswith("chr_"):
        return None
    chore_id = thread_id.removeprefix("chr_").strip()
    return chore_id or None


def job_thread_id(kind: JobKind, job_id: str) -> str:
    """Return the normalized thread id for one job."""

    return task_thread_id(job_id) if kind == "task" else chore_thread_id(job_id)


def task_path(
    toolang_root: Path,
    agent_name: str,
    task_id: str,
    *,
    lifecycle: JobLifecycle = "ready",
) -> Path:
    """Return one local task path."""

    return _work_dir(toolang_root, agent_name, kind="task", lifecycle=lifecycle) / _relative_id_path(task_id)


def chore_path(
    toolang_root: Path,
    agent_name: str,
    chore_id: str,
    *,
    lifecycle: JobLifecycle = "ready",
) -> Path:
    """Return one local chore path."""

    return _work_dir(toolang_root, agent_name, kind="chore", lifecycle=lifecycle) / _relative_id_path(chore_id)


def job_path(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: JobKind,
    job_id: str,
    lifecycle: JobLifecycle = "ready",
) -> Path:
    """Return one local job path."""

    return task_path(toolang_root, agent_name, job_id, lifecycle=lifecycle) if kind == "task" else chore_path(toolang_root, agent_name, job_id, lifecycle=lifecycle)


def list_tasks(
    toolang_root: Path,
    agent_name: str,
    *,
    lifecycle: JobLifecycle = "ready",
    include_archived: bool = False,
) -> tuple[TaskEntry, ...]:
    """List local tasks for one agent."""

    if include_archived:
        items = [*_task_entries(toolang_root, agent_name, lifecycle="ready"), *_task_entries(toolang_root, agent_name, lifecycle="archived")]
        return tuple(sorted(items, key=lambda item: str(item.path)))
    return tuple(sorted(_task_entries(toolang_root, agent_name, lifecycle=lifecycle), key=lambda item: str(item.path)))


def list_archived_tasks(toolang_root: Path, agent_name: str) -> tuple[TaskEntry, ...]:
    """List archived local tasks for one agent."""

    return list_tasks(toolang_root, agent_name, lifecycle="archived")


def list_draft_tasks(toolang_root: Path, agent_name: str) -> tuple[TaskEntry, ...]:
    """List draft local tasks for one agent."""

    return list_tasks(toolang_root, agent_name, lifecycle="draft")


def find_task(
    toolang_root: Path,
    agent_name: str,
    task_id: str,
    *,
    lifecycle: JobLifecycle | None = "ready",
    include_archived: bool = False,
) -> TaskEntry | None:
    """Find one local task by stable task id."""

    lifecycles = _find_lifecycles(lifecycle, include_archived=include_archived)
    for item_lifecycle in lifecycles:
        for entry in list_tasks(toolang_root, agent_name, lifecycle=item_lifecycle):
            if entry.document.task_id() == task_id:
                return entry
    return None


def find_archived_task(toolang_root: Path, agent_name: str, task_id: str) -> TaskEntry | None:
    """Find one archived local task by stable task id."""

    return find_task(toolang_root, agent_name, task_id, lifecycle="archived")


def list_chores(
    toolang_root: Path,
    agent_name: str,
    *,
    lifecycle: JobLifecycle = "ready",
    include_archived: bool = False,
) -> tuple[ChoreEntry, ...]:
    """List local chores for one agent."""

    if include_archived:
        items = [*_chore_entries(toolang_root, agent_name, lifecycle="ready"), *_chore_entries(toolang_root, agent_name, lifecycle="archived")]
        return tuple(sorted(items, key=lambda item: str(item.path)))
    return tuple(sorted(_chore_entries(toolang_root, agent_name, lifecycle=lifecycle), key=lambda item: str(item.path)))


def list_archived_chores(toolang_root: Path, agent_name: str) -> tuple[ChoreEntry, ...]:
    """List archived local chores for one agent."""

    return list_chores(toolang_root, agent_name, lifecycle="archived")


def list_draft_chores(toolang_root: Path, agent_name: str) -> tuple[ChoreEntry, ...]:
    """List draft local chores for one agent."""

    return list_chores(toolang_root, agent_name, lifecycle="draft")


def find_chore(
    toolang_root: Path,
    agent_name: str,
    chore_id: str,
    *,
    lifecycle: JobLifecycle | None = "ready",
    include_archived: bool = False,
) -> ChoreEntry | None:
    """Find one local chore by stable chore id."""

    lifecycles = _find_lifecycles(lifecycle, include_archived=include_archived)
    for item_lifecycle in lifecycles:
        for entry in list_chores(toolang_root, agent_name, lifecycle=item_lifecycle):
            if entry.document.chore_id() == chore_id:
                return entry
    return None


def find_archived_chore(toolang_root: Path, agent_name: str, chore_id: str) -> ChoreEntry | None:
    """Find one archived local chore by stable chore id."""

    return find_chore(toolang_root, agent_name, chore_id, lifecycle="archived")


def find_job(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: JobKind,
    job_id: str,
    lifecycle: JobLifecycle | None = "ready",
) -> TaskEntry | ChoreEntry | None:
    """Find one local job by stable id."""

    if kind == "task":
        return find_task(toolang_root, agent_name, job_id, lifecycle=lifecycle)
    return find_chore(toolang_root, agent_name, job_id, lifecycle=lifecycle)


def load_task_text(toolang_root: Path, agent_name: str, task_id: str) -> str:
    """Load one task definition."""

    entry = find_task(toolang_root, agent_name, task_id, lifecycle=None)
    if entry is None:
        raise FileNotFoundError(f"task not found: {task_id}")
    return entry.path.read_text(encoding="utf-8")


def load_chore_text(toolang_root: Path, agent_name: str, chore_id: str) -> str:
    """Load one chore definition."""

    entry = find_chore(toolang_root, agent_name, chore_id, lifecycle=None)
    if entry is None:
        raise FileNotFoundError(f"chore not found: {chore_id}")
    return entry.path.read_text(encoding="utf-8")


def create_task_text(
    toolang_root: Path,
    agent_name: str,
    text: str,
    *,
    lifecycle: JobLifecycle = "ready",
) -> Path:
    """Create one validated task definition with an auto-generated id path."""

    document = TaskFile.parse_text(text).model_copy(
        update={"id": allocate_job_id(toolang_root, agent_name)}
    )
    path = task_path(toolang_root, agent_name, document.task_id(), lifecycle=lifecycle)
    document.save(path)
    return path


def create_chore_text(
    toolang_root: Path,
    agent_name: str,
    text: str,
    *,
    lifecycle: JobLifecycle = "ready",
) -> Path:
    """Create one validated chore definition with an auto-generated id path."""

    document = ChoreFile.parse_text(text).model_copy(
        update={"id": allocate_job_id(toolang_root, agent_name)}
    )
    path = chore_path(toolang_root, agent_name, document.chore_id(), lifecycle=lifecycle)
    document.save(path)
    return path


def clone_task(toolang_root: Path, agent_name: str, task_id: str) -> Path:
    """Clone one task definition into a new ready task."""

    entry = find_task(toolang_root, agent_name, task_id, lifecycle=None)
    if entry is None:
        raise FileNotFoundError(f"task not found: {task_id}")
    document = entry.document.model_copy(update={"id": allocate_job_id(toolang_root, agent_name)})
    path = task_path(toolang_root, agent_name, document.task_id(), lifecycle="ready")
    document.save(path)
    return path


def clone_chore(toolang_root: Path, agent_name: str, chore_id: str) -> Path:
    """Clone one chore definition into a new ready chore."""

    entry = find_chore(toolang_root, agent_name, chore_id, lifecycle=None)
    if entry is None:
        raise FileNotFoundError(f"chore not found: {chore_id}")
    document = entry.document.model_copy(update={"id": allocate_job_id(toolang_root, agent_name)})
    path = chore_path(toolang_root, agent_name, document.chore_id(), lifecycle="ready")
    document.save(path)
    return path


def update_task_text(toolang_root: Path, agent_name: str, task_id: str, text: str) -> Path:
    """Replace one task definition, preserving its stable id and lifecycle."""

    entry = find_task(toolang_root, agent_name, task_id, lifecycle=None)
    if entry is None:
        raise FileNotFoundError(f"task not found: {task_id}")
    document = _task_with_existing_id(TaskFile.parse_text(text), task_id=entry.document.task_id())
    return save_task_entry(toolang_root, agent_name, entry, document)


def update_chore_text(toolang_root: Path, agent_name: str, chore_id: str, text: str) -> Path:
    """Replace one chore definition, preserving its stable id and lifecycle."""

    entry = find_chore(toolang_root, agent_name, chore_id, lifecycle=None)
    if entry is None:
        raise FileNotFoundError(f"chore not found: {chore_id}")
    document = _chore_with_existing_id(ChoreFile.parse_text(text), chore_id=entry.document.chore_id())
    return save_chore_entry(toolang_root, agent_name, entry, document)


def save_task_entry(
    toolang_root: Path,
    agent_name: str,
    entry: TaskEntry,
    document: TaskFile,
) -> Path:
    """Save a task entry in its current lifecycle folder."""

    document = _task_with_existing_id(document, task_id=entry.document.task_id())
    target = task_path(toolang_root, agent_name, document.task_id(), lifecycle=entry.lifecycle)
    document.save(target)
    if entry.path != target:
        entry.path.unlink(missing_ok=True)
        _prune_empty_parents(entry.path.parent, stop=_work_dir(toolang_root, agent_name, kind="task", lifecycle=entry.lifecycle))
    return target


def save_chore_entry(
    toolang_root: Path,
    agent_name: str,
    entry: ChoreEntry,
    document: ChoreFile,
) -> Path:
    """Save a chore entry in its current lifecycle folder."""

    document = _chore_with_existing_id(document, chore_id=entry.document.chore_id())
    target = chore_path(toolang_root, agent_name, document.chore_id(), lifecycle=entry.lifecycle)
    document.save(target)
    if entry.path != target:
        entry.path.unlink(missing_ok=True)
        _prune_empty_parents(entry.path.parent, stop=_work_dir(toolang_root, agent_name, kind="chore", lifecycle=entry.lifecycle))
    return target


def move_task_lifecycle(
    toolang_root: Path,
    agent_name: str,
    task_id: str,
    *,
    lifecycle: JobLifecycle,
) -> Path | None:
    """Move one task definition to another lifecycle folder."""

    entry = find_task(toolang_root, agent_name, task_id, lifecycle=None)
    if entry is None:
        return None
    return _move_entry(toolang_root, agent_name, kind="task", entry=entry, lifecycle=lifecycle)


def move_chore_lifecycle(
    toolang_root: Path,
    agent_name: str,
    chore_id: str,
    *,
    lifecycle: JobLifecycle,
) -> Path | None:
    """Move one chore definition to another lifecycle folder."""

    entry = find_chore(toolang_root, agent_name, chore_id, lifecycle=None)
    if entry is None:
        return None
    return _move_entry(toolang_root, agent_name, kind="chore", entry=entry, lifecycle=lifecycle)


def archive_task(toolang_root: Path, agent_name: str, task_id: str) -> Path | None:
    """Archive one task by id."""

    return move_task_lifecycle(toolang_root, agent_name, task_id, lifecycle="archived")


def archive_chore(toolang_root: Path, agent_name: str, chore_id: str) -> Path | None:
    """Archive one chore by id."""

    return move_chore_lifecycle(toolang_root, agent_name, chore_id, lifecycle="archived")


def ready_task(toolang_root: Path, agent_name: str, task_id: str) -> Path | None:
    """Move one task to the ready folder."""

    return move_task_lifecycle(toolang_root, agent_name, task_id, lifecycle="ready")


def ready_chore(toolang_root: Path, agent_name: str, chore_id: str) -> Path | None:
    """Move one chore to the ready folder."""

    return move_chore_lifecycle(toolang_root, agent_name, chore_id, lifecycle="ready")


def draft_task(toolang_root: Path, agent_name: str, task_id: str) -> Path | None:
    """Move one task to the draft folder."""

    return move_task_lifecycle(toolang_root, agent_name, task_id, lifecycle="draft")


def draft_chore(toolang_root: Path, agent_name: str, chore_id: str) -> Path | None:
    """Move one chore to the draft folder."""

    return move_chore_lifecycle(toolang_root, agent_name, chore_id, lifecycle="draft")


def remove_archived_task(toolang_root: Path, agent_name: str, task_id: str) -> bool:
    """Remove one archived task definition."""

    entry = find_archived_task(toolang_root, agent_name, task_id)
    if entry is None:
        return False
    entry.path.unlink()
    _prune_empty_parents(entry.path.parent, stop=_work_dir(toolang_root, agent_name, kind="task", lifecycle="archived"))
    return True


def remove_archived_chore(toolang_root: Path, agent_name: str, chore_id: str) -> bool:
    """Remove one archived chore definition."""

    entry = find_archived_chore(toolang_root, agent_name, chore_id)
    if entry is None:
        return False
    entry.path.unlink()
    _prune_empty_parents(entry.path.parent, stop=_work_dir(toolang_root, agent_name, kind="chore", lifecycle="archived"))
    return True


def ready_job_entries(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: JobKind | None = None,
) -> tuple[TaskEntry | ChoreEntry, ...]:
    """Return ready job entries scanned from ready folders."""

    items: list[TaskEntry | ChoreEntry] = []
    if kind in {None, "task"}:
        items.extend(list_tasks(toolang_root, agent_name))
    if kind in {None, "chore"}:
        items.extend(list_chores(toolang_root, agent_name))
    return tuple(sorted(items, key=lambda item: str(item.path)))


def _task_entries(
    toolang_root: Path,
    agent_name: str,
    *,
    lifecycle: JobLifecycle,
) -> Iterable[TaskEntry]:
    root = _work_dir(toolang_root, agent_name, kind="task", lifecycle=lifecycle)
    if not root.exists():
        return ()
    return tuple(
        TaskEntry(
            name=str(path.relative_to(root).with_suffix("")),
            path=path,
            document=TaskFile.load(
                path,
                id_factory=(lambda: allocate_job_id(toolang_root, agent_name)),
                persist_id=True,
            ),
            lifecycle=lifecycle,
        )
        for path in sorted(root.rglob("*.md"))
    )


def _chore_entries(
    toolang_root: Path,
    agent_name: str,
    *,
    lifecycle: JobLifecycle,
) -> Iterable[ChoreEntry]:
    root = _work_dir(toolang_root, agent_name, kind="chore", lifecycle=lifecycle)
    if not root.exists():
        return ()
    return tuple(
        ChoreEntry(
            name=str(path.relative_to(root).with_suffix("")),
            path=path,
            document=ChoreFile.load(
                path,
                id_factory=(lambda: allocate_job_id(toolang_root, agent_name)),
                persist_id=True,
            ),
            lifecycle=lifecycle,
        )
        for path in sorted(root.rglob("*.md"))
    )


def _work_dir(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: JobKind,
    lifecycle: JobLifecycle,
) -> Path:
    home = toolang_root / "agents" / agent_name
    bucket = "tasks" if kind == "task" else "chores"
    if lifecycle == "ready":
        return home / bucket
    if lifecycle == "draft":
        return home / "drafts" / bucket
    return home / "archive" / bucket


def _move_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: JobKind,
    entry: TaskEntry | ChoreEntry,
    lifecycle: JobLifecycle,
) -> Path:
    if isinstance(entry, TaskEntry):
        item_id = entry.document.task_id()
    else:
        item_id = entry.document.chore_id()
    target = job_path(toolang_root, agent_name, kind=kind, job_id=item_id, lifecycle=lifecycle)
    target.parent.mkdir(parents=True, exist_ok=True)
    if entry.path != target:
        entry.path.replace(target)
        _prune_empty_parents(
            entry.path.parent,
            stop=_work_dir(toolang_root, agent_name, kind=kind, lifecycle=entry.lifecycle),
        )
    return target


def _find_lifecycles(
    lifecycle: JobLifecycle | None,
    *,
    include_archived: bool,
) -> tuple[JobLifecycle, ...]:
    if lifecycle is not None:
        if include_archived and lifecycle == "ready":
            return ("ready", "archived")
        return (lifecycle,)
    return ("ready", "draft", "archived")


def _relative_id_path(value: str) -> Path:
    text = value.strip()
    if not text or "/" in text or "\\" in text or text in {".", ".."}:
        raise ValueError(f"invalid id: {value}")
    return Path(text).with_suffix(".md")


def _task_with_existing_id(document: TaskFile, *, task_id: str) -> TaskFile:
    if document.id is None:
        return document.model_copy(update={"id": task_id})
    if document.id != task_id:
        raise ValueError(f"task id cannot be changed: {task_id} -> {document.id}")
    return document


def _chore_with_existing_id(document: ChoreFile, *, chore_id: str) -> ChoreFile:
    if document.id is None:
        return document.model_copy(update={"id": chore_id})
    if document.id != chore_id:
        raise ValueError(f"chore id cannot be changed: {chore_id} -> {document.id}")
    return document


def _prune_empty_parents(path: Path, *, stop: Path) -> None:
    current = path
    while current != stop and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _id_state_path(toolang_root: Path, agent_name: str) -> Path:
    return toolang_root / "agents" / agent_name / ".runtime" / "ids.json"


def allocate_job_id(toolang_root: Path, agent_name: str) -> str:
    """Allocate one local job id for an agent."""

    return allocate_id(
        _id_state_path(toolang_root, agent_name),
        family=LOCAL_ID_FAMILY,
        exists=lambda value: _job_id_exists(toolang_root, agent_name, value),
    ).value


def _job_id_exists(toolang_root: Path, agent_name: str, value: str) -> bool:
    for path in _job_document_paths(toolang_root, agent_name):
        try:
            post = frontmatter.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if path.stem == value:
            return True
        if str(post.metadata.get("id", "")).strip() == value:
            return True
    return False


def _job_document_paths(toolang_root: Path, agent_name: str) -> Iterable[Path]:
    roots = (
        _work_dir(toolang_root, agent_name, kind="task", lifecycle="ready"),
        _work_dir(toolang_root, agent_name, kind="chore", lifecycle="ready"),
        _work_dir(toolang_root, agent_name, kind="task", lifecycle="draft"),
        _work_dir(toolang_root, agent_name, kind="chore", lifecycle="draft"),
        _work_dir(toolang_root, agent_name, kind="task", lifecycle="archived"),
        _work_dir(toolang_root, agent_name, kind="chore", lifecycle="archived"),
    )
    for root in roots:
        if not root.exists():
            continue
        yield from sorted(root.rglob("*.md"))


def _display_title(title: str | None, body: str, *, fallback: str) -> str:
    text = (title or "").strip()
    if text:
        return text
    for line in body.splitlines():
        candidate = line.strip().lstrip("#").strip()
        if candidate:
            return candidate[:80]
    return fallback
