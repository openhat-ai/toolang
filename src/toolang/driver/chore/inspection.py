"""Aggregate authored, scheduler, and execution state for chore inspection."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import fields
from pathlib import Path
from typing import Mapping, Protocol

from toolang.common.layout import AgentLayout
from toolang.execution.types import ErrorMessage, ErrorRef
from .records import ChoreRecord
from .schemas import ChoreDetail, ChoreInfo, ChoreRuntimeInfo, LastRunInfo
from toolang.catalog.chores import Chore, inspect_chore_sources
from .store import open_chore_store


class ChoreRun(Protocol):
    """Execution fields required by chore inspection."""

    id: str
    thread: str
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: ErrorMessage | ErrorRef | None


class ChoreInspection:
    """Inspect authored chores with scheduler and execution state in one batch."""

    def __init__(
        self,
        *,
        home: Path,
        definitions: tuple[Chore, ...],
        records: dict[str, ChoreRecord],
        latest_runs: dict[str, ChoreRun],
        error_messages: Mapping[str, str] | None = None,
    ) -> None:
        self.home = home
        self.definitions = definitions
        self.records = records
        self.latest_runs = latest_runs
        self.error_messages = dict(error_messages or {})

    @classmethod
    def load(
        cls,
        *,
        layout: AgentLayout,
        runs: Iterable[ChoreRun],
        error_messages: Mapping[str, str] | None = None,
        definitions: tuple[Chore, ...] | None = None,
    ) -> ChoreInspection:
        """Load definitions and checkpoints from the owning stores."""

        if not layout.chore_store.is_file():
            records = ()
        else:
            chore_store = open_chore_store(layout, read_only=True)
            try:
                records = chore_store.list()
            finally:
                chore_store.close()
        latest_runs: dict[str, ChoreRun] = {}
        for run in runs:
            current = latest_runs.get(str(run.thread))
            if current is None or run.created_at > current.created_at:
                latest_runs[str(run.thread)] = run
        return cls(
            home=layout.agent_home,
            definitions=inspect_chore_sources(layout)
            if definitions is None
            else definitions,
            records={record.id: record for record in records},
            latest_runs=latest_runs,
            error_messages=error_messages,
        )

    def list(self) -> tuple[ChoreInfo, ...]:
        return tuple(self._info(chore) for chore in self.definitions)

    def get(self, reference: str) -> Chore | None:
        return next(
            (
                chore
                for chore in self.definitions
                if reference in {*self._identity(chore)[:2], chore.name}
            ),
            None,
        )

    def detail(self, chore: Chore) -> ChoreDetail:
        info = self._info(chore)
        return ChoreDetail(
            **{field.name: getattr(info, field.name) for field in fields(info)},
            body=chore.body,
        )

    def _info(self, chore: Chore) -> ChoreInfo:
        chore_id, ref, thread_id = self._identity(chore)
        record, last_run = self._state(chore_id=chore_id, thread_id=thread_id)
        synced = (
            chore.source_error is None
            and record is not None
            and record.body_hash == chore.body_hash
            and record.schedule_hash == chore.schedule_hash
        )
        try:
            path = str(chore.path.relative_to(self.home))
        except ValueError:
            path = str(chore.path)
        return ChoreInfo(
            id=chore_id,
            name=chore.name,
            ref=ref,
            title=chore.display_title,
            path=path,
            line=chore.line,
            schedule=chore.schedule,
            source_error=chore.source_error,
            runtime=ChoreRuntimeInfo(
                thread_id=thread_id,
                synced=synced,
                status=record.status if record else None,
                active_run_id=record.active_run if record else None,
                pending_at=record.pending_at if record else None,
                next_scheduled_at=record.next_scheduled_at if record else None,
                last_run=last_run,
                scheduler_error=record.error if record else None,
                updated_at=record.updated_at if record else None,
            ),
        )

    def _identity(self, chore: Chore) -> tuple[str | None, str, str | None]:
        try:
            chore_id = chore.id
        except ValueError:
            if chore.source_error is None:
                raise
            return None, chore.path.relative_to(self.home).as_posix(), None
        return chore_id, chore.ref, chore.thread_id

    def _state(
        self, *, chore_id: str | None, thread_id: str | None
    ) -> tuple[ChoreRecord | None, LastRunInfo | None]:
        record = self.records.get(chore_id) if chore_id is not None else None
        run = self.latest_runs.get(thread_id) if thread_id is not None else None
        last_run = (
            LastRunInfo(
                id=run.id,
                status=run.status,
                started_at=run.started_at,
                finished_at=run.finished_at,
                error=self.error_messages.get(run.id)
                or (run.error if isinstance(run.error, str) else None),
            )
            if run is not None
            else None
        )
        return record, last_run
