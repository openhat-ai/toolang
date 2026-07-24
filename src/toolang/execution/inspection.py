"""Store-backed execution inspection."""

from __future__ import annotations

from .records import RunRecord
from .schemas import RunDetail, RunInfo, ThreadDetail, ThreadInfo
from .store import RunStore
from .types import RunStatus


class ExecutionInspection:
    """Inspect durable execution truth through caller-facing schemas."""

    def __init__(self, store: RunStore) -> None:
        self.store = store

    def list_threads(
        self,
        *,
        limit: int | None = 50,
        origin: str | None = None,
        channel: str | None = None,
        status: str | None = None,
    ) -> list[ThreadInfo]:
        """Return filtered thread summaries in most-recently-updated order."""

        threads = self.store.list_threads()
        runs_by_thread = {
            thread.thread_id: self.store.list_thread_history_chronological(
                thread_id=thread.thread_id
            )
            for thread in threads
        }
        controls_by_run = {
            run.id: self.store.list_run_controls(run_id=run.id)
            for runs in runs_by_thread.values()
            for run in runs
        }
        items = [
            ThreadInfo.from_records(
                thread,
                runs_by_thread[thread.thread_id],
                controls_by_run=controls_by_run,
            )
            for thread in threads
        ]
        filtered = [
            item
            for item in items
            if (origin is None or item.origin == origin)
            and (channel is None or item.channel == channel)
            and (status is None or item.status == status)
        ]
        ordered = sorted(filtered, key=lambda item: item.updated_at, reverse=True)
        return ordered if limit is None else ordered[:limit]

    def thread_info(self, thread_id: str) -> ThreadInfo | None:
        """Return one thread summary when durable thread truth exists."""

        thread = self.store.get_thread(thread_id=thread_id)
        if thread is None:
            return None
        runs = self.store.list_thread_history_chronological(thread_id=thread_id)
        return ThreadInfo.from_records(
            thread,
            runs,
            controls_by_run={
                run.id: self.store.list_run_controls(run_id=run.id) for run in runs
            },
        )

    def thread_detail(
        self, thread_id: str, *, limit: int | None = 50
    ) -> ThreadDetail | None:
        """Return one thread and its most recent run details."""

        info = self.thread_info(thread_id)
        if info is None:
            return None
        runs = list(
            self.store.list_thread_history_chronological(
                thread_id=thread_id,
                limit=None,
            )
        )
        visible_runs = runs if limit is None else runs[-limit:]
        return ThreadDetail.from_info(
            info,
            runs=[self._run_detail(run) for run in visible_runs],
        )

    def list_runs(
        self,
        *,
        limit: int | None = 50,
        thread_id: str | None = None,
        status: RunStatus | None = None,
    ) -> list[RunInfo]:
        """Return run information from durable truth."""

        runs = self.store.list_runs(limit=limit, thread_id=thread_id, status=status)
        steps_by_run = self.store.list_steps_for_runs(
            run_ids=tuple(item.id for item in runs)
        )
        return [
            RunInfo.from_record(
                run,
                controls=self.store.list_run_controls(run_id=run.id),
                steps=steps_by_run.get(run.id, ()),
            )
            for run in runs
        ]

    def run_info(self, run: RunRecord) -> RunInfo:
        """Return one caller-facing run summary."""

        return RunInfo.from_record(
            run,
            controls=self.store.list_run_controls(run_id=run.id),
            steps=self.store.list_steps(run_id=run.id),
        )

    def run_detail(self, run_id: str) -> RunDetail | None:
        """Return one complete run detail when it exists."""

        run = self.store.get_run(run_id=run_id)
        return self._run_detail(run) if run is not None else None

    def _run_detail(self, run: RunRecord) -> RunDetail:
        steps = self.store.list_steps(run_id=run.id)
        return RunDetail.from_record(
            run,
            steps=steps,
            controls=self.store.list_run_controls(run_id=run.id),
            model_calls={
                step.path: self.store.rebuild_model_call(step)
                for step in steps
                if step.kind == "model" and "call" in step.given
            },
        )
