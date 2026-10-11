"""Pure authored chore definitions and filesystem authoring operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from dateutil.rrule import rrulestr
import frontmatter

from toolang.common.files import atomic_write_text, file_write_lock
from toolang.common.ids import chore_id

from .common import normalize_meta
from .errors import CatalogConflictError, CatalogNotFoundError

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class ChoreFile:
    """A named definition; identity and progress are never written into its body."""

    name: str
    path: Path | None
    content: str
    meta: Mapping[str, object]
    body: str

    def __post_init__(self) -> None:
        chore_id(self.name)
        for key in ("title", "schedule"):
            value = self.meta.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"chore {key} must be non-empty text when provided")
        if self.schedule is not None:
            rrulestr(self.schedule, dtstart=datetime(2026, 1, 1, tzinfo=timezone.utc))

    @property
    def id(self) -> str:
        return chore_id(self.name)

    @property
    def ref(self) -> str:
        return f"chore/{self.name}"

    @property
    def title(self) -> str | None:
        return _optional_text(self.meta, "title")

    @property
    def schedule(self) -> str | None:
        return _optional_text(self.meta, "schedule")

    @classmethod
    def parse(
        cls,
        content: str,
        *,
        name: str | None = None,
        path: Path | None = None,
    ) -> ChoreFile:
        if name is None:
            if path is None:
                raise ValueError("chore name or source path is required")
            name = path.stem
        if path is not None and name != path.stem:
            raise ValueError("chore name must match its filename")
        post = frontmatter.loads(content)
        return cls(
            name=name,
            path=path,
            content=content,
            meta=normalize_meta(post.metadata),
            body=post.content,
        )

    def with_meta(self, meta: Mapping[str, object]) -> ChoreFile:
        normalized = normalize_meta(meta)
        content = frontmatter.dumps(frontmatter.Post(self.body, None, **normalized))
        return replace(self, content=content, meta=normalized)

    def with_body(self, body: str) -> ChoreFile:
        content = frontmatter.dumps(frontmatter.Post(body, None, **dict(self.meta)))
        return replace(self, content=content, body=body)

    def patch(self, changes: Mapping[str, str | None]) -> ChoreFile:
        unsupported = set(changes).difference({"title", "body", "schedule"})
        if unsupported:
            raise ValueError(
                f"unsupported chore fields: {', '.join(sorted(unsupported))}"
            )
        meta = dict(self.meta)
        body = self.body
        for field, value in changes.items():
            if field == "body":
                body = value or ""
            elif value is None:
                meta.pop(field, None)
            else:
                meta[field] = value
        return self.with_meta(meta).with_body(body)


class AuthoredChores:
    """CRUD for one definition layer, addressed by name, ref, or derived ID."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def lock_path(self) -> Path:
        return self.directory / ".chores.lock"

    def write_lock(self) -> AbstractContextManager[None]:
        return file_write_lock(self.lock_path)

    def list(self) -> tuple[ChoreFile, ...]:
        return self._read_consistently(self._list_files)

    def get(self, reference: str) -> ChoreFile | None:
        def read() -> ChoreFile | None:
            entries = self._list_files()
            matches = [
                item for item in entries if reference in (item.name, item.ref, item.id)
            ]
            if len(matches) > 1:
                raise CatalogConflictError(f"ambiguous chore reference: {reference}")
            return next(iter(matches), None)

        return self._read_consistently(read)

    def contains_name(self, name: str) -> bool:
        return self.get(name) is not None

    def create(self, chore: ChoreFile) -> ChoreFile:
        with self.write_lock():
            if self.contains_name(chore.name):
                raise CatalogConflictError(f"chore already exists: {chore.name}")
            return self._write(chore, path=self.path(chore.name))

    def update(self, chore: ChoreFile) -> ChoreFile:
        with self.write_lock():
            existing = self.get(chore.name)
            if existing is None or existing.path is None:
                raise CatalogNotFoundError(f"chore not found: {chore.name}")
            return self._write(chore, path=existing.path)

    def remove(self, reference: str) -> ChoreFile:
        with self.write_lock():
            chore = self.get(reference)
            if chore is None or chore.path is None:
                raise CatalogNotFoundError(f"chore not found: {reference}")
            chore.path.unlink()
            return chore

    def path(self, name: str) -> Path:
        chore_id(name)
        return self.directory / "chores" / f"{name}.md"

    def _write(self, chore: ChoreFile, *, path: Path) -> ChoreFile:
        atomic_write_text(path, chore.content)
        return ChoreFile.parse(chore.content, path=path)

    def _read_consistently(self, read: Callable[[], _T]) -> _T:
        if self._lock_file_exists():
            with self.write_lock():
                return read()
        try:
            result = read()
        except OSError:
            if not self._lock_file_exists():
                raise
        else:
            if not self._lock_file_exists():
                return result
        with self.write_lock():
            return read()

    def _lock_file_exists(self) -> bool:
        return self.lock_path.exists() or self.lock_path.is_symlink()

    def _list_files(self) -> tuple[ChoreFile, ...]:
        directory = self.directory / "chores"
        if not directory.is_dir():
            return ()
        return tuple(
            ChoreFile.parse(path.read_text(encoding="utf-8"), path=path)
            for path in sorted(directory.glob("*.md"))
        )


def _optional_text(meta: Mapping[str, object], key: str) -> str | None:
    value = meta.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None
