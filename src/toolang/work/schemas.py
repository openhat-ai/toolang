"""Caller-facing chore protocol schemas."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

from toolang.catalog.chore import ChoreFile
from .records import ChoreRecord
from .state import (
    Chore,
    chore_display_title,
    chore_remote_ref,
    chore_remote_status,
    chore_thread_id,
)
from .types import ChoreStatus


@dataclass(frozen=True, slots=True)
class LastRunInfo:
    """Small run summary embedded in chore runtime state."""

    id: str
    status: str
    started_at: str | None
    finished_at: str | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ChoreRuntimeInfo:
    """Runtime state attached to one authored chore."""

    thread_id: str
    last_run: LastRunInfo | None
    next_run_at: str | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ChoreInfo:
    """Summary of one authored chore."""

    id: str
    name: str
    ref: str
    status: ChoreStatus | None
    title: str
    path: str
    updated_at: str
    runtime: ChoreRuntimeInfo
    schedule: str | None = None
    remote_ref: str | None = None
    remote_status: str | None = None

    @classmethod
    def from_chore(
        cls,
        chore: ChoreFile | Chore,
        *,
        home: Path,
        record: ChoreRecord | None,
        last_run: LastRunInfo | None,
    ) -> ChoreInfo:
        """Build caller-facing chore information from authored and durable state."""

        if chore.path is None:
            path = chore.source if isinstance(chore, Chore) else "inline"
            updated_at = record.updated_at if record is not None else ""
        else:
            try:
                path = str(chore.path.relative_to(home))
            except ValueError:
                path = str(chore.path)
            updated_at = datetime.fromtimestamp(
                chore.path.stat().st_mtime_ns / 1_000_000_000, tz=timezone.utc
            ).isoformat()
        return cls(
            id=chore.id,
            name=chore.name,
            ref=chore.ref,
            status=record.status if record is not None else None,
            schedule=chore.schedule,
            remote_ref=chore_remote_ref(chore),
            remote_status=chore_remote_status(chore),
            title=chore_display_title(chore, fallback=chore.id),
            path=path,
            updated_at=updated_at,
            runtime=ChoreRuntimeInfo(
                thread_id=chore_thread_id(chore),
                last_run=last_run,
                next_run_at=record.next_run_at if record is not None else None,
                error=record.error if record is not None else None,
            ),
        )


@dataclass(frozen=True, slots=True)
class ChoreDetail(ChoreInfo):
    """Complete representation of one authored chore."""

    body: str = ""

    @classmethod
    def from_chore(
        cls,
        chore: ChoreFile | Chore,
        *,
        home: Path,
        record: ChoreRecord | None,
        last_run: LastRunInfo | None,
    ) -> ChoreDetail:
        """Build complete caller-facing chore detail."""

        info = ChoreInfo.from_chore(
            chore,
            home=home,
            record=record,
            last_run=last_run,
        )
        return cls(
            **{item.name: getattr(info, item.name) for item in fields(ChoreInfo)},
            body=chore.body,
        )
