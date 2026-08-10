"""Authored task and chore files."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from dateutil.rrule import rrulestr
import frontmatter

from toolang.common.files import atomic_write_text, file_write_lock

from .common import normalize_meta
from .types import (
    DEFAULT_CHORE_SCHEDULE,
    JOB_KINDS,
    JOB_STAGES,
    JobKind,
    JobStage,
)
from .errors import CatalogConflictError, CatalogNotFoundError, DuplicateJobIdError


@dataclass(frozen=True, slots=True)
class JobFile:
    """One parsed authored task or chore file."""

    path: Path | None
    content: str
    kind: JobKind
    stage: JobStage
    meta: Mapping[str, object]
    body: str

    def __post_init__(self) -> None:
        _validate_job(self, require_id=False)

    @property
    def optional_id(self) -> str | None:
        """Return the stable authored id when the source declares one."""

        return _optional_meta_text(self.meta, "id")

    @property
    def id(self) -> str:
        value = self.optional_id
        if value is None:
            raise ValueError("job id is required")
        return value

    @property
    def title(self) -> str | None:
        return _optional_meta_text(self.meta, "title")

    @property
    def schedule(self) -> str:
        if self.kind != "chore":
            raise ValueError("only chores have a schedule")
        return _optional_meta_text(self.meta, "schedule") or DEFAULT_CHORE_SCHEDULE

    @classmethod
    def parse(
        cls,
        content: str,
        *,
        kind: JobKind,
        stage: JobStage = "ready",
        job_id: str | None = None,
        path: Path | None = None,
    ) -> JobFile:
        """Parse source content and project caller-resolved identity metadata."""

        post = frontmatter.loads(content)
        meta = normalize_meta(post.metadata)
        authored_id = _optional_meta_text(meta, "id")
        if authored_id is not None and job_id is not None and authored_id != job_id:
            raise ValueError(
                f"job id does not match the expected id: {authored_id!r} != {job_id!r}"
            )
        if authored_id is None and job_id is not None:
            meta["id"] = job_id
        return cls(
            path=path,
            content=content,
            kind=kind,
            stage=stage,
            meta=meta,
            body=post.content,
        )

    def with_meta(self, meta: Mapping[str, object]) -> JobFile:
        normalized = normalize_meta(meta)
        content = frontmatter.dumps(frontmatter.Post(self.body, None, **normalized))
        return replace(self, content=content, meta=normalized)

    def with_body(self, body: str) -> JobFile:
        content = frontmatter.dumps(frontmatter.Post(body, None, **dict(self.meta)))
        return replace(self, content=content, body=body)

    def patch(self, changes: Mapping[str, str | None]) -> JobFile:
        """Apply caller-selected editable fields to this authored job."""

        allowed = {"title", "body"}
        if self.kind == "chore":
            allowed.add("schedule")
        unsupported = set(changes).difference(allowed)
        if unsupported:
            fields = ", ".join(sorted(unsupported))
            raise ValueError(f"unsupported {self.kind} fields: {fields}")
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


class AuthoredJobs:
    """CRUD for authored jobs below one explicitly supplied agent home."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def lock_path(self) -> Path:
        return self.directory / ".authored-jobs.lock"

    def write_lock(self) -> AbstractContextManager[None]:
        """Return the shared lock used by all authored-job mutations."""

        return file_write_lock(self.lock_path)

    def list(
        self,
        *,
        kind: JobKind | None = None,
        stage: JobStage = "ready",
    ) -> tuple[JobFile, ...]:
        with self.write_lock():
            _validate_stage(stage)
            kinds = JOB_KINDS if kind is None else (kind,)
            jobs = tuple(
                job
                for current_kind in kinds
                for job in self._list_stage(current_kind, stage)
            )
            _ensure_unique_ids(jobs)
            return tuple(sorted(jobs, key=_job_sort_key))

    def get(
        self,
        kind: JobKind,
        job_id: str,
        *,
        stage: JobStage | None = "ready",
    ) -> JobFile | None:
        with self.write_lock():
            _validate_kind(kind)
            _validate_id(job_id)
            jobs = self._list_all()
            _ensure_unique_ids(jobs)
            return next(
                (
                    job
                    for job in jobs
                    if job.kind == kind
                    and job.optional_id == job_id
                    and (stage is None or job.stage == stage)
                ),
                None,
            )

    def contains_id(self, job_id: str) -> bool:
        """Return whether any task or chore in any stage owns an id."""

        with self.write_lock():
            _validate_id(job_id)
            jobs = self._list_all()
            _ensure_unique_ids(jobs)
            return any(job.optional_id == job_id for job in jobs)

    def create(self, job: JobFile) -> JobFile:
        _validate_job(job, require_id=True)
        with self.write_lock():
            jobs = self._list_all()
            _ensure_unique_ids(jobs)
            if any(item.optional_id == job.id for item in jobs):
                raise CatalogConflictError(f"authored job id already exists: {job.id}")
            target = self.path(job.kind, job.id, stage=job.stage)
            if target.exists():
                raise CatalogConflictError(
                    f"authored job path already exists: {target}"
                )
            return self._write(job, path=target)

    def update(self, job: JobFile) -> JobFile:
        _validate_job(job, require_id=True)
        with self.write_lock():
            jobs = self._list_all()
            _ensure_unique_ids(jobs)
            existing = next(
                (item for item in jobs if item.optional_id == job.id),
                None,
            )
            if existing is None or existing.path is None:
                raise CatalogNotFoundError(f"authored job not found: {job.id}")
            if existing.kind != job.kind:
                raise CatalogConflictError(
                    f"job id {job.id!r} belongs to {existing.kind}, not {job.kind}"
                )
            if existing.stage != job.stage:
                raise ValueError("use move() to change an authored job stage")
            return self._write(job, path=existing.path)

    def move(self, kind: JobKind, job_id: str, stage: JobStage) -> JobFile:
        _validate_stage(stage)
        with self.write_lock():
            job = self.get(kind, job_id, stage=None)
            if job is None or job.path is None:
                raise CatalogNotFoundError(f"authored {kind} not found: {job_id}")
            if job.stage == stage:
                return job
            target = self._directory(job.kind, stage) / job.path.name
            if target.exists():
                raise CatalogConflictError(
                    f"authored job path already exists: {target}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            job.path.replace(target)
            _prune_empty_parents(
                job.path.parent,
                stop=self._directory(job.kind, job.stage),
            )
            return JobFile.parse(
                target.read_text(encoding="utf-8"),
                kind=job.kind,
                stage=stage,
                job_id=job.id,
                path=target,
            )

    def remove(self, kind: JobKind, job_id: str) -> JobFile:
        with self.write_lock():
            job = self.get(kind, job_id, stage=None)
            if job is None or job.path is None:
                raise CatalogNotFoundError(f"authored {kind} not found: {job_id}")
            job.path.unlink()
            _prune_empty_parents(
                job.path.parent,
                stop=self._directory(job.kind, job.stage),
            )
            return job

    def assign_missing_ids(
        self,
        id_factory: Callable[[], str],
        *,
        stage: JobStage | None = None,
    ) -> tuple[JobFile, ...]:
        """Persist ids for manually authored files that do not declare one."""

        with self.write_lock():
            jobs = (
                self._list_all()
                if stage is None
                else tuple(
                    job for kind in JOB_KINDS for job in self._list_stage(kind, stage)
                )
            )
            _ensure_unique_ids(jobs)
            known_ids = {job.optional_id for job in jobs if job.optional_id is not None}
            assigned: list[JobFile] = []
            for job in sorted(jobs, key=_job_sort_key):
                if job.optional_id is not None:
                    continue
                job_id = id_factory()
                _validate_id(job_id)
                if job_id in known_ids:
                    raise CatalogConflictError(
                        f"generated job id already exists: {job_id}"
                    )
                if job.path is None:
                    raise ValueError("authored job path is required")
                saved = self._write(
                    job.with_meta({**job.meta, "id": job_id}),
                    path=job.path,
                )
                known_ids.add(job_id)
                assigned.append(saved)
            return tuple(assigned)

    def path(self, kind: JobKind, job_id: str, *, stage: JobStage) -> Path:
        _validate_kind(kind)
        _validate_stage(stage)
        _validate_id(job_id)
        return self._directory(kind, stage) / f"{job_id}.md"

    def _write(self, job: JobFile, *, path: Path) -> JobFile:
        _validate_job(job, require_id=True)
        atomic_write_text(path, job.content)
        return JobFile.parse(
            path.read_text(encoding="utf-8"),
            kind=job.kind,
            stage=job.stage,
            job_id=job.id,
            path=path,
        )

    def _list_all(self) -> tuple[JobFile, ...]:
        return tuple(
            job
            for stage in JOB_STAGES
            for kind in JOB_KINDS
            for job in self._list_stage(kind, stage)
        )

    def _list_stage(self, kind: JobKind, stage: JobStage) -> tuple[JobFile, ...]:
        directory = self._directory(kind, stage)
        if not directory.is_dir():
            return ()
        return tuple(
            JobFile.parse(
                path.read_text(encoding="utf-8"),
                kind=kind,
                stage=stage,
                path=path,
            )
            for path in sorted(directory.glob("*.md"))
        )

    def _directory(self, kind: JobKind, stage: JobStage) -> Path:
        _validate_kind(kind)
        _validate_stage(stage)
        bucket = "tasks" if kind == "task" else "chores"
        if stage == "ready":
            return self.directory / bucket
        if stage == "draft":
            return self.directory / "drafts" / bucket
        return self.directory / "archive" / bucket


def _validate_job(job: JobFile, *, require_id: bool) -> None:
    _validate_kind(job.kind)
    _validate_stage(job.stage)
    job_id = job.optional_id
    if require_id and job_id is None:
        raise ValueError("job id is required")
    if job_id is not None:
        _validate_id(job_id)
    _validate_optional_text(job.meta, "title")
    if job.kind == "chore":
        rrulestr(job.schedule, dtstart=datetime(2026, 1, 1, tzinfo=timezone.utc))


def _ensure_unique_ids(jobs: tuple[JobFile, ...]) -> None:
    by_id: dict[str, list[JobFile]] = {}
    for job in jobs:
        job_id = job.optional_id
        if job_id is None:
            continue
        by_id.setdefault(job_id, []).append(job)
    for job_id, matches in sorted(by_id.items()):
        if len(matches) < 2:
            continue
        ordered = sorted(matches, key=_modified_sort_key)
        latest = ordered[-1]
        existing = ordered[0]
        assert latest.path is not None and existing.path is not None
        raise DuplicateJobIdError(
            job_id,
            path=latest.path,
            existing_path=existing.path,
        )


def _modified_sort_key(job: JobFile) -> tuple[int, str]:
    if job.path is None:
        return (0, "")
    return (job.path.stat().st_mtime_ns, str(job.path))


def _job_sort_key(job: JobFile) -> tuple[str, str, str]:
    return (job.kind, job.optional_id or "", str(job.path or ""))


def _validate_kind(kind: JobKind) -> None:
    if kind not in JOB_KINDS:
        raise ValueError(f"unsupported job kind: {kind}")


def _validate_stage(stage: JobStage) -> None:
    if stage not in JOB_STAGES:
        raise ValueError(f"unsupported job stage: {stage}")


def _validate_id(value: str) -> None:
    text = value.strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError(f"invalid job id: {value!r}")


def _validate_optional_text(meta: Mapping[str, object], key: str) -> None:
    value = meta.get(key)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"job {key} must be non-empty text when provided")


def _optional_meta_text(meta: Mapping[str, object], key: str) -> str | None:
    value = meta.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _required_meta_text(meta: Mapping[str, object], key: str) -> str:
    value = _optional_meta_text(meta, key)
    if value is None:
        raise ValueError(f"job {key} is required")
    return value


def _prune_empty_parents(path: Path, *, stop: Path) -> None:
    current = path
    while current != stop and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent
