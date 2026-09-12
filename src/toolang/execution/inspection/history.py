"""Caller-facing read access to durable execution history."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import cast
from pydantic import TypeAdapter

from toolang.lang.types import Value
from toolang.base.types.message import Part, TextPart
from toolang.base.types.run import ModelCall
from ..records import (
    RunControlPayload,
    ControlRecord,
    RunRecord,
    StepRecord,
    StoredModelStepGiven,
    ThreadRecord,
)
from ..schemas import (
    CompactionOutput,
    HistoryCursor,
    RunDetail,
    RunInfo,
    ThreadDetail,
    ThreadInfo,
    ThreadPage,
    ThreadPageCursor,
)
from .views import RunView, ThreadView
from ..store import RunStore
from ..types import (
    ControlRef,
    ErrorMessage,
    ErrorRef,
    FieldRef,
    Local,
    Output,
    Pointer,
    RunRef,
    RunStatus,
    StepRef,
    ThreadRef,
)
from ..values import parts_from_local

_CURSOR = TypeAdapter(HistoryCursor)
_THREAD_CURSOR = TypeAdapter(ThreadPageCursor)


class RunHistory:
    """Read durable run and thread truth through caller-facing schemas."""

    def __init__(self, store: RunStore) -> None:
        self._store = store

    def thread_page(self, *, limit: int = 20, cursor: str | None = None) -> ThreadPage:
        """Page captured Thread IDs, reading metadata as of each page."""

        with self._store.read_transaction():
            if cursor is None:
                _validate_page_limit(limit)
                scope = ThreadPageCursor(self._store.history_thread_ids(), limit, 0)
            else:
                scope = _THREAD_CURSOR.validate_json(cursor)
                _validate_page_limit(scope.limit)
                if not 0 <= scope.offset < len(scope.entries):
                    raise ValueError("history cursor offset is out of range")
            ids = scope.entries[scope.offset : scope.offset + scope.limit]
            threads = []
            for identity in ids:
                record = self._store.get_thread(thread_id=identity)
                if record is None:
                    raise KeyError(identity)
                threads.append(record)
            offset = scope.offset + len(ids)
            return ThreadPage(
                tuple(threads),
                _THREAD_CURSOR.dump_json(replace(scope, offset=offset)).decode()
                if offset < len(scope.entries)
                else None,
            )

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
        with self._store.read_transaction():
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

        views = self._store.thread_views(tuple(thread.id for thread in threads))
        runs_by_thread = {thread.id: views[thread.id].tree() for thread in threads}
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
                    head=views[thread.id].head,
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
        with self._store.read_transaction():
            thread = self._store.get_thread(thread_id=thread_id)
            if thread is None:
                return None
            view = self._store.thread_view(thread_id)
            runs = list(view.tree())
            controls_by_run = self._store.list_run_controls_for_runs(
                run_ids=tuple(run.id for run in runs)
            )
            info = ThreadInfo.from_records(
                thread,
                runs,
                head=view.head,
                input_parts=(
                    self._input_parts(runs[0], controls_by_run.get(runs[0].id, ()))
                    if runs
                    else ()
                ),
            )
            visible_runs = runs
            if run_limit is not None:
                visible_runs = runs[-run_limit:] if run_limit else []
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
        with self._store.read_transaction():
            runs = self._store.list_runs(
                limit=limit, thread_id=thread_id, status=status
            )
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
                input_parts=self._input_parts(
                    run,
                    controls_by_run.get(run.id, ()),
                ),
            )
            for run in runs
        ]

    def get_run(self, run_id: str) -> RunDetail | None:
        """Return one complete run detail when it exists."""

        with self._store.read_transaction():
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
                input_parts=self._input_parts(run, controls),
            )

    def get_output(self, run: RunRef | str) -> Output | None:
        """Resolve typed output without loading execution details or model calls."""

        with self._store.read_transaction():
            record = self._store.get_run(run_id=str(run))
            if record is None:
                raise KeyError(str(run))
            return (
                self._store.resolve_output(record.output)
                if record.output is not None
                else None
            )

    def get_model_call(self, step: StepRef | str) -> ModelCall:
        """Reconstruct exactly the normalized call persisted at a Model Step."""

        ref = StepRef.parse(step)
        with self._store.read_transaction():
            record = self._store.get_step(ref=ref)
            if record is None:
                raise KeyError(str(ref))
            if not isinstance(record.given, StoredModelStepGiven):
                raise ValueError(f"not a model step: {ref}")
            return self._store.rebuild_model_calls((record,))[ref]

    def get_compaction(self, thread: ThreadRef | str) -> CompactionOutput | None:
        """Find the latest applicable full-prefix summary, skipping interval tests."""

        target = ThreadRef.parse(thread)
        with self._store.read_transaction():
            if self._store.get_thread(thread_id=str(target)) is None:
                raise KeyError(str(target))
            compact_id = f"compact_{target}"
            if self._store.get_thread(thread_id=compact_id) is None:
                return None
            _, _, target_members = self._store.history_thread_members(str(target))
            roots = tuple(ref for ref, root in target_members.items() if ref == root)
            if len(roots) < 2:
                return None
            _, _, members = self._store.history_thread_members(compact_id)
            for run_id, root_id in reversed(members.items()):
                if run_id != root_id:
                    continue
                run = self._require_run(run_id)
                if run.status == "succeeded" and run.output is not None:
                    output = self.get_output(run.id)
                    assert output is not None
                    raw = output.local.value
                    if not isinstance(raw, Mapping):
                        continue
                    value = cast(Mapping[str, object], raw)
                    control = self._store.get_run_control(run_id=run.id, index=0)
                    assert control is not None and isinstance(
                        control.payload, RunControlPayload
                    )
                    input = control.payload.input
                    summary = value.get("summary")
                    if not (
                        value.get("thread") == input.get("thread") == str(target)
                        and value.get("begin") in (None, roots[0])
                        and value.get("end") == input.get("end")
                        and value.get("end") in roots[1:]
                        and isinstance(summary, str)
                        and summary.strip()
                        and (
                            input.get("begin") in (None, roots[0])
                            or input.get("previous")
                        )
                    ):
                        continue
                    return CompactionOutput(
                        FieldRef.from_path(RunRef(run.id), "output"), output
                    )
            return None

    def thread_view(
        self,
        thread: ThreadRef | str,
        *,
        begin: RunRef | None = None,
        end: RunRef | None = None,
        limit: int | None = None,
        reverse: bool = False,
        include_children: bool = True,
    ) -> ThreadView:
        """Capture a half-open root Run range, optionally including its children."""

        _validate_page_limit(limit)
        with self._store.read_transaction():
            record, head, members = self._store.history_thread_members(str(thread))
            target = ThreadRef(record.id)
            ids = tuple(ref for ref, root in members.items() if ref == root)
            selected = _bounded_ids(ids, begin, end)
            selected_roots = set(selected)
            members = {
                ref: root
                for ref, root in members.items()
                if root in selected_roots and (include_children or ref == root)
            }
            scope = HistoryCursor(
                target,
                selected,
                self._store.history_versions(tuple(members)),
                limit or max(len(selected), 1),
                reverse=reverse,
                thread=record,
                head=head,
                members=members,
            )
            return self._thread_page(scope)

    def run_view(
        self,
        run: RunRef | str,
        *,
        begin: StepRef | None = None,
        end: StepRef | None = None,
        limit: int | None = None,
        reverse: bool = False,
    ) -> RunView:
        """Read Steps and their dependencies, without child Run internals.

        Step bounds select exact existing Step IDs and are exclusive at end.
        Unbounded reads also include all raw owned controls for inspection.
        """

        _validate_page_limit(limit)
        target = RunRef.parse(run)
        for boundary in (begin, end):
            if boundary is not None and boundary.run != target:
                raise ValueError(f"step boundary belongs to another run: {boundary}")
        with self._store.read_transaction():
            record = self._store.get_run(run_id=str(target))
            if record is None:
                raise KeyError(str(target))
            entries, related = self._store.history_entries(str(target))
            step_ids = _bounded_ids(tuple(related), begin, end)
            selected = step_ids if begin is not None or end is not None else entries
            refs = {str(target), str(record.control), str(record.state), *selected}
            for ref in step_ids:
                refs.update(related[ref])
            # A root retry can delete/recreate child facts. Freeze the ancestry,
            # not just the reused child Step ID or its lifecycle timestamps.
            if record.parent is not None:
                refs.update(self._store.run_ancestry(run_id=record.parent.run_id))
            scope = HistoryCursor(
                target,
                selected,
                self._store.history_versions(tuple(sorted(refs))),
                limit or max(len(selected), 1),
                reverse=reverse,
            )
            return self._run_page(scope)

    def next_page(self, cursor: str) -> ThreadView | RunView:
        """Continue a fixed read, rejecting replacement of any captured fact."""

        scope = _CURSOR.validate_json(cursor)
        _validate_page_limit(scope.limit)
        if not 0 <= scope.offset < len(scope.entries):
            raise ValueError("history cursor offset is out of range")
        with self._store.read_transaction():
            self._store.validate_history(scope.versions)
            if isinstance(scope.target, ThreadRef):
                return self._thread_page(scope)
            return self._run_page(scope)

    def _thread_page(self, scope: HistoryCursor) -> ThreadView:
        ids, cursor = _page_ids(scope)
        assert scope.thread is not None and scope.head is not None
        selected = set(ids)
        members = tuple(
            self._require_run(ref)
            for ref, root in scope.members.items()
            if root in selected
        )
        by_id = {run.id: run for run in members}
        return ThreadView(
            scope.thread,
            scope.head,
            tuple(by_id[ref] for ref in ids),
            members,
            cursor,
        )

    def _run_page(self, scope: HistoryCursor) -> RunView:
        ids, cursor = _page_ids(scope)
        entries: list[StepRecord | ControlRecord] = []
        controls: set[ControlRef] = set()
        for ref in ids:
            record = self._store.get_record(Pointer.parse(ref))
            assert isinstance(record, StepRecord | ControlRecord)
            entries.append(record)
            if isinstance(record, StepRecord):
                controls.update((record.state, *record.preceded_by))
                if record.aborted_by is not None:
                    controls.add(record.aborted_by)
        dependencies: list[ControlRecord] = []
        for control_ref in sorted(controls, key=lambda item: str(item)):
            if str(control_ref) not in ids:
                control = self._store.get_record(Pointer(control_ref))
                assert isinstance(control, ControlRecord)
                dependencies.append(control)
        return RunView(
            self._require_run(str(scope.target)),
            tuple(entries),
            tuple(dependencies),
            cursor,
        )

    def _require_run(self, ref: str) -> RunRecord:
        record = self._store.get_run(run_id=ref)
        if record is None:
            raise KeyError(ref)
        return record

    def _error_message(self, error: ErrorMessage | ErrorRef | None) -> str | None:
        if error is None:
            return None
        return self._store.resolve_error(error)

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
                control.payload, RunControlPayload
            ):
                continue
            authored = control.payload.authored_input
            if authored is not None and "_" in authored:
                return (TextPart(authored["_"]),)
            if "_" in control.payload.input:
                value = self._store.resolve_value(control.payload.input["_"])
                return parts_from_local(Local(cast(Value, value)))
            return ()
        return ()


def _model_steps(steps: Sequence[StepRecord]) -> tuple[StepRecord, ...]:
    return tuple(step for step in steps if isinstance(step.given, StoredModelStepGiven))


def _validate_limit(limit: int | None) -> None:
    if limit is not None and limit < 0:
        raise ValueError("history limit must not be negative")


def _validate_page_limit(limit: int | None) -> None:
    if limit is not None and (isinstance(limit, bool) or limit <= 0):
        raise ValueError("history page limit must be positive")


def _bounded_ids(
    ids: tuple[str, ...],
    begin: RunRef | StepRef | None,
    end: RunRef | StepRef | None,
) -> tuple[str, ...]:
    start = ids.index(str(begin)) if begin is not None else 0
    stop = ids.index(str(end)) if end is not None else len(ids)
    if start > stop:
        raise ValueError("history begin must not follow end")
    return ids[start:stop]


def _page_ids(scope: HistoryCursor) -> tuple[tuple[str, ...], str | None]:
    ordered = scope.entries[::-1] if scope.reverse else scope.entries
    ids = ordered[scope.offset : scope.offset + scope.limit]
    offset = scope.offset + len(ids)
    cursor = (
        _CURSOR.dump_json(replace(scope, offset=offset)).decode()
        if offset < len(ordered)
        else None
    )
    return (ids[::-1] if scope.reverse else ids), cursor
