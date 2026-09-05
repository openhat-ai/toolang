"""Caller-facing read access to durable execution history."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Literal

from toolang.base.types.message import Part, TextPart
from toolang.base.types.run import ModelCall
from .records import (
    PreparationControlPayload,
    ControlRecord,
    RunRecord,
    StepRecord,
    StoredModelStepGiven,
    ThreadRecord,
)
from .schemas import RunDetail, RunInfo, ThreadDetail, ThreadInfo
from .store import RunStore
from .types import ControlRef, ErrorMessage, ErrorRef, RunStatus, StepRef
from .values import parts_from_local


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
        items = self.describe_threads(self._store.list_threads())
        filtered = [
            item
            for item in items
            if (origin is None or item.origin == origin)
            and (channel is None or item.channel == channel)
            and (status is None or item.status == status)
        ]
        ordered = sorted(filtered, key=lambda item: item.updated_at, reverse=True)
        return ordered if limit is None else ordered[:limit]

    def describe_threads(
        self,
        threads: Sequence[ThreadRecord],
    ) -> list[ThreadInfo]:
        """Build summaries for a caller-selected sequence of Thread records."""

        views = self._store.thread_views()
        runs_by_thread = {
            thread.id: views.tree(views.history(thread.id)) for thread in threads
        }
        run_ids = tuple(
            dict.fromkeys(run.id for runs in runs_by_thread.values() for run in runs)
        )
        controls_by_run = self._store.list_run_controls_for_runs(
            run_ids=run_ids,
        )
        items: list[ThreadInfo] = []
        for thread in threads:
            runs = runs_by_thread.get(thread.id, ())
            items.append(
                ThreadInfo.from_records(
                    thread,
                    runs,
                    head=views.head(thread.id),
                    input_parts=(
                        self._input_parts(
                            runs[0],
                            controls_by_run.get(runs[0].id, ()),
                        )
                        if runs
                        else ()
                    ),
                )
            )
        return items

    def get_thread(
        self, thread_id: str, *, run_limit: int | None = 50
    ) -> ThreadDetail | None:
        """Return one thread and its most recent run details."""

        _validate_limit(run_limit)
        thread = self._store.get_thread(thread_id=thread_id)
        if thread is None:
            return None
        views = self._store.thread_views()
        runs = list(views.tree(views.history(thread_id)))
        controls_by_run = self._store.list_run_controls_for_runs(
            run_ids=tuple(run.id for run in runs)
        )
        info = ThreadInfo.from_records(
            thread,
            runs,
            head=views.head(thread_id),
            input_parts=(
                self._input_parts(runs[0], controls_by_run.get(runs[0].id, ()))
                if runs
                else ()
            ),
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
                    root_run_id=self._store.root_run_id(run_id=run.id),
                    error_message=self._error_message(run.error),
                    ejection_scope=self._ejection_scope(run.ejected_by),
                    input_parts=self._input_parts(
                        run,
                        controls_by_run.get(run.id, ()),
                    ),
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
        return self.describe_runs(runs)

    def describe_runs(
        self,
        runs: Sequence[RunRecord],
        *,
        steps_by_run: Mapping[str, Sequence[StepRecord]] | None = None,
    ) -> list[RunInfo]:
        """Build summaries, reusing caller-supplied visible Steps when present."""

        if steps_by_run is None:
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
                root_run_id=self._store.root_run_id(run_id=run.id),
                error_message=self._error_message(run.error),
                ejection_scope=self._ejection_scope(run.ejected_by),
                input_parts=self._input_parts(
                    run,
                    controls_by_run.get(run.id, ()),
                ),
            )
            for run in runs
        ]

    def get_run(self, run_id: str) -> RunDetail | None:
        """Return one complete run detail when it exists."""

        run = self._store.get_run(run_id=run_id)
        if run is None:
            return None
        steps = self._store.list_steps(run_id=run.id)
        controls = self._store.list_run_controls(run_id=run.id)
        return RunDetail.from_record(
            run,
            controls=controls,
            steps=steps,
            model_calls=self._store.rebuild_model_calls(_model_steps(steps)),
            root_run_id=self._store.root_run_id(run_id=run.id),
            error_message=self._error_message(run.error),
            ejection_scope=self._ejection_scope(run.ejected_by),
            input_parts=self._input_parts(run, controls),
        )

    def get_run_result(self, run_id: str) -> RunDetail | None:
        """Return one run detail with its durable output fully resolved."""

        detail = self.get_run(run_id)
        if detail is None or detail.output is None:
            return detail
        return replace(detail, output=self._store.resolve_local(detail.output))

    def latest_thread_result(self, thread_id: str) -> RunDetail | None:
        """Return the newest succeeded root run with a nonempty result."""

        if self._store.get_thread(thread_id=thread_id) is None:
            raise KeyError(thread_id)
        runs = self._store.list_thread_history_chronological(thread_id=thread_id)
        for run in reversed(runs):
            if (
                run.parent is not None
                or run.status != "succeeded"
                or run.output is None
                or not self._store.run_output(run_id=run.id)
            ):
                continue
            return self.get_run_result(run.id)
        return None

    def _error_message(self, error: ErrorMessage | ErrorRef | None) -> str | None:
        if error is None:
            return None
        return self._store.resolve_error(error)

    def _ejection_scope(
        self, ref: ControlRef | None
    ) -> Literal["run", "thread"] | None:
        if ref is None:
            return None
        return self._store.control_scope(ref)

    def _model_calls(
        self, steps_by_run: Mapping[str, Sequence[StepRecord]]
    ) -> dict[StepRef, ModelCall]:
        return self._store.rebuild_model_calls(
            tuple(
                step for steps in steps_by_run.values() for step in _model_steps(steps)
            )
        )

    def _input_parts(
        self,
        run: RunRecord,
        controls: Sequence[ControlRecord],
    ) -> tuple[Part, ...]:
        for control in reversed(controls):
            if control.index > run.control.index or not isinstance(
                control.payload, PreparationControlPayload
            ):
                continue
            authored = control.payload.authored_input
            if authored is not None and authored._ is not None:
                return (TextPart(authored._),)
            locals_value = control.payload.locals
            if locals_value is None:
                continue
            primary = next(
                (local for local in locals_value if local.name == "_"),
                None,
            )
            return (
                parts_from_local(self._store.resolve_local(primary))
                if primary is not None
                else ()
            )
        return ()


def _model_steps(steps: Sequence[StepRecord]) -> tuple[StepRecord, ...]:
    return tuple(step for step in steps if isinstance(step.given, StoredModelStepGiven))


def _validate_limit(limit: int | None) -> None:
    if limit is not None and limit < 0:
        raise ValueError("history limit must not be negative")
