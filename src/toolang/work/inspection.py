"""Aggregate authored, scheduler, and execution state for chore inspection."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Mapping, Protocol

from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.common.layout import AgentLayout
from toolang.execution.types import ErrorMessage, ErrorRef
from .records import ChoreRecord
from .schemas import ChoreDetail, ChoreInfo, LastRunInfo
from toolang.lang.ast import Program
from .state import Chore, chore_thread_id, load_chores, merge_chores, program_chores
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
        catalog: AuthoredChores,
        home: Path,
        definitions: tuple[Chore, ...],
        records: dict[str, ChoreRecord],
        latest_runs: dict[str, ChoreRun],
        error_messages: Mapping[str, str] | None = None,
    ) -> None:
        self.catalog = catalog
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
        read_only: bool = False,
        program: Program | None = None,
        error_messages: Mapping[str, str] | None = None,
    ) -> ChoreInspection:
        """Load one consistent inspection snapshot from the owning stores."""

        catalog = AuthoredChores(layout.home)
        if read_only and not layout.chore_store.is_file():
            records = ()
        else:
            chore_store = open_chore_store(layout, read_only=read_only)
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
            catalog=catalog,
            home=layout.home,
            definitions=tuple(
                merge_chores(
                    load_chores(layout), program_chores(program) if program else ()
                ).values()
            ),
            records={record.chore_id: record for record in records},
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
                if reference in {chore.id, chore.name, chore.ref}
            ),
            None,
        )

    def detail(self, chore: ChoreFile | Chore) -> ChoreDetail:
        record, last_run = self._state(chore)
        return ChoreDetail.from_chore(
            chore,
            home=self.home,
            record=record,
            last_run=last_run,
        )

    def _info(self, chore: ChoreFile | Chore) -> ChoreInfo:
        record, last_run = self._state(chore)
        return ChoreInfo.from_chore(
            chore,
            home=self.home,
            record=record,
            last_run=last_run,
        )

    def _state(
        self, chore: ChoreFile | Chore
    ) -> tuple[ChoreRecord | None, LastRunInfo | None]:
        record = self.records.get(chore.id)
        run = self.latest_runs.get(chore_thread_id(chore))
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
