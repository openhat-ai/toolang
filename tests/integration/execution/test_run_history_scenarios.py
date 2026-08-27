"""Caller-facing durable execution history scenarios."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.execution_fixtures import (
    project_run_end,
    project_run_start,
    project_step,
)
from toolang.base.types.message import Message, TextPart
from toolang.common.ids import IdIssuer
from toolang.execution.history import RunHistory
from toolang.execution.schemas import ThreadControlRefData
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.types import ControlRef, Local, ThreadPrefix, Pointer


def test_run_history_batches_thread_and_run_summaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_history",
            thread_id="term_history",
            origin="chat",
            input=Message.user("hello"),
        )
        project_run_end(store, run_id=run.id)
        history = RunHistory(store)
        monkeypatch.setattr(
            store,
            "list_thread_history_chronological",
            lambda **_kwargs: pytest.fail("history must batch thread reads"),
        )
        monkeypatch.setattr(
            store,
            "list_run_controls",
            lambda **_kwargs: pytest.fail("history must batch summary controls"),
        )

        threads = history.list_threads(limit=None)
        runs = history.list_runs(limit=None)
        thread = history.get_thread(run.thread)

        assert [(item.id, item.run_count) for item in threads] == [("term_history", 1)]
        assert [item.id for item in runs] == ["run_history"]
        assert thread is not None
        assert [item.id for item in thread.runs] == ["run_history"]
    finally:
        store.close()


def test_run_history_projects_fork_and_rewind_from_one_snapshot(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    ids = IdIssuer(tmp_path / "ids.json")
    manager = ThreadManager(store, ids)
    try:
        source = manager.create(prefix=ThreadPrefix.TERM)
        first = project_run_start(
            store,
            run_id="run_first",
            thread_id=source,
            origin="chat",
            input=Message.user("first"),
        )
        project_run_end(store, run_id=first.id)
        second = project_run_start(
            store,
            run_id="run_second",
            thread_id=source,
            origin="chat",
            input=Message.user("second"),
        )
        project_run_end(store, run_id=second.id)
        forked = manager.fork(thread_id=source, run_id=first.id)
        manager.rewind(thread_id=source, run_id=second.id)

        histories = store.list_thread_histories_chronological(
            thread_ids=(source, forked)
        )
        projected = {
            item.id: item for item in RunHistory(store).list_threads(limit=None)
        }

        assert [run.id for run in histories[source]] == [first.id]
        assert [run.id for run in histories[forked]] == [first.id]
        assert projected[source].run_count == 1
        assert projected[forked].run_count == 1
    finally:
        store.close()


def test_run_history_zero_detail_limit_returns_only_thread_summary(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_summary",
            thread_id="term_summary",
            origin="chat",
            input=Message.user("hello"),
        )
        project_run_end(store, run_id=run.id)

        thread = RunHistory(store).get_thread(run.thread, run_limit=0)

        assert thread is not None
        assert thread.run_count == 1
        assert thread.runs == []
    finally:
        store.close()


def test_run_history_resolves_run_output_for_run_and_thread_details(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_output",
            thread_id="term_output",
            origin="chat",
            input=Message.user("hello"),
        )
        step = project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart("ignored"), TextPart("result")),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        project_run_end(
            store,
            run_id=run.id,
            output=Local.typed("Part", Pointer.step(step.path, 1), "_", 0),
        )

        history = RunHistory(store)
        detail = history.get_run(run.id)
        thread = history.get_thread(run.thread)

        assert store.run_output(run_id=run.id) == (TextPart("result"),)
        assert detail is not None
        expected = Local.typed("Part", Pointer.step(step.path, 1), "_", 0)
        assert detail.output == expected
        assert thread is not None
        assert thread.runs[0].output == expected
    finally:
        store.close()


def test_run_history_resolves_pass_through_control_output(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_passthrough",
            thread_id="term_passthrough",
            origin="chat",
            input=Message.user("unchanged"),
        )
        project_run_end(
            store,
            run_id=run.id,
            output=Pointer.control(run.id, 0, "_"),
        )

        stored = store.get_run(run_id=run.id)
        detail = RunHistory(store).get_run(run.id)

        assert stored is not None
        assert stored.control == ControlRef(run.id, 0)
        assert stored.output == Local.typed(
            "Part[]", Pointer.control(run.id, 0, "_"), "_", 0
        )
        assert store.run_output(run_id=run.id) == Message.user("unchanged").parts
        assert detail is not None
        assert detail.output == Local.typed(
            "Part[]", Pointer.control(run.id, 0, "_"), "_", 0
        )
    finally:
        store.close()


def test_resolve_local_rejects_a_pointer_to_a_different_type(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_mismatch",
            thread_id="term_mismatch",
            origin="chat",
            input=Message.user("not a number"),
        )

        with pytest.raises(TypeError, match="Number"):
            store.resolve_local(
                Local.typed(
                    "Number",
                    Pointer.control(run.id, 0, "_"),
                    "_",
                    0,
                )
            )
    finally:
        store.close()


def test_run_history_reads_thread_ejection_scope_from_the_control_record(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        first = project_run_start(
            store,
            run_id="first",
            thread_id="run_thread",
            origin="chat",
            input=Message.user("first"),
        )
        project_run_end(store, run_id=first.id)
        second = project_run_start(
            store,
            run_id="second",
            thread_id="run_thread",
            origin="chat",
            input=Message.user("second"),
        )
        project_run_end(store, run_id=second.id)
        thread = store.get_thread(thread_id="run_thread")
        assert thread is not None
        store.rewind_thread(
            thread_id="run_thread",
            anchor=first.id,
            request_id=None,
            expected_head=thread.head,
            created_at="2026-01-01T00:00:05Z",
        )

        rewind_ejected = RunHistory(store).get_run(second.id)
        assert rewind_ejected is not None
        assert rewind_ejected.ejected == ThreadControlRefData(
            thread="run_thread",
            index=1,
        )
    finally:
        store.close()
