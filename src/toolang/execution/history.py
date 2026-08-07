"""Caller-facing read access to durable execution history."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from toolang.base.types.run import ModelCall
from .records import StepRecord
from .schemas import RunDetail, RunInfo, ThreadDetail, ThreadInfo
from .store import RunStore
from .types import RunStatus, StepPath


class RunHistory:
    """Read durable run and thread truth through caller-facing schemas."""

    def __init__(self, store: RunStore) -> None:
        self._store = store

    def list_threads(
        self,
        *,
        limit: int | None = 50,
        origin: str | None = None,
        channel: str | None = None,
        status: str | None = None,
    ) -> list[ThreadInfo]:
        """Return filtered thread summaries in most-recently-updated order."""

        _validate_limit(limit)
        threads = self._store.list_threads()
        runs_by_thread = self._store.list_thread_histories_chronological(
            thread_ids=tuple(thread.thread_id for thread in threads)
        )
        run_ids = tuple(
            dict.fromkeys(run.id for runs in runs_by_thread.values() for run in runs)
        )
        controls_by_run = self._store.list_run_controls_for_runs(
            run_ids=run_ids,
            kind="start",
        )
        items = [
            ThreadInfo.from_records(
                thread,
                runs_by_thread.get(thread.thread_id, ()),
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

    def get_thread(
        self, thread_id: str, *, run_limit: int | None = 50
    ) -> ThreadDetail | None:
        """Return one thread and its most recent run details."""

        _validate_limit(run_limit)
        thread = self._store.get_thread(thread_id=thread_id)
        if thread is None:
            return None
        runs = list(
            self._store.list_thread_histories_chronological(
                thread_ids=(thread_id,),
            ).get(thread_id, ())
        )
        controls_by_run = self._store.list_run_controls_for_runs(
            run_ids=tuple(run.id for run in runs)
        )
        info = ThreadInfo.from_records(
            thread,
            runs,
            controls_by_run=controls_by_run,
        )
        visible_runs = (
            runs if run_limit is None else [] if run_limit == 0 else runs[-run_limit:]
        )
        steps_by_run = self._store.list_steps_for_runs(
            run_ids=tuple(run.id for run in visible_runs)
        )
        model_calls = self._model_calls(steps_by_run)
        return ThreadDetail.from_info(
            info,
            runs=[
                RunDetail.from_record(
                    run,
                    controls=controls_by_run.get(run.id, ()),
                    steps=steps_by_run.get(run.id, ()),
                    model_calls=model_calls,
                )
                for run in visible_runs
            ],
        )

    def list_runs(
        self,
        *,
        limit: int | None = 50,
        thread_id: str | None = None,
        status: RunStatus | None = None,
    ) -> list[RunInfo]:
        """Return run information from durable truth."""

        _validate_limit(limit)
        runs = self._store.list_runs(limit=limit, thread_id=thread_id, status=status)
        steps_by_run = self._store.list_steps_for_runs(
            run_ids=tuple(item.id for item in runs)
        )
        controls_by_run = self._store.list_run_controls_for_runs(
            run_ids=tuple(item.id for item in runs)
        )
        return [
            RunInfo.from_record(
                run,
                controls=controls_by_run.get(run.id, ()),
                steps=steps_by_run.get(run.id, ()),
            )
            for run in runs
        ]

    def get_run(self, run_id: str) -> RunDetail | None:
        """Return one complete run detail when it exists."""

        run = self._store.get_run(run_id=run_id)
        if run is None:
            return None
        steps = self._store.list_steps(run_id=run.id)
        return RunDetail.from_record(
            run,
            controls=self._store.list_run_controls(run_id=run.id),
            steps=steps,
            model_calls=self._store.rebuild_model_calls(_model_steps(steps)),
        )

    def _model_calls(
        self, steps_by_run: Mapping[str, Sequence[StepRecord]]
    ) -> dict[StepPath, ModelCall]:
        return self._store.rebuild_model_calls(
            tuple(
                step for steps in steps_by_run.values() for step in _model_steps(steps)
            )
        )


def _model_steps(steps: Sequence[StepRecord]) -> tuple[StepRecord, ...]:
    return tuple(
        step for step in steps if step.kind == "model" and "call" in step.given
    )


def _validate_limit(limit: int | None) -> None:
    if limit is not None and limit < 0:
        raise ValueError("history limit must not be negative")
