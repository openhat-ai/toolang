"""Caller-facing durable execution history scenarios."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from tests.support.execution_fixtures import (
    project_run_end,
    project_run_start,
    project_step,
)
from toolang.base.types.message import Message, TextPart
from toolang.common.ids import IdIssuer
from toolang.execution.inspection.history import RunHistory
from toolang.execution.store import RunStore
from toolang.execution.threads import ThreadManager
from toolang.execution.types import (
    ControlRef,
    FieldRef,
    ThreadKind,
    Output,
    value_for_type,
)


def test_run_history_batches_thread_and_run_summaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_history",
            thread_id="chat_history",
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
        thread = history.get_thread(str(run.thread))

        assert [(item.id, item.run_count) for item in threads] == [("chat_history", 1)]
        assert [item.id for item in runs] == ["run_history"]
        assert thread is not None
        assert [item.id for item in thread.runs] == ["run_history"]
    finally:
        store.close()


def test_thread_order_uses_parent_completion_after_its_last_child(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        thread = "chat_parent"
        store.create_thread(thread_id=thread, created_at="2026-10-20T00:00:00Z")
        parent = project_run_start(
            store,
            run_id="run_parent",
            thread_id=thread,
            input=Message.user("Parent"),
            created_at="2026-10-20T01:00:00Z",
        )
        step = project_step(
            store,
            run_id=parent.id,
            step_index=0,
            kind="run",
            status="running",
            input=(),
            output=None,
            started_at="2026-10-20T02:00:00Z",
            finished_at=None,
        )
        child = project_run_start(
            store,
            run_id="run_child",
            thread_id=thread,
            parent=step.ref,
            input=Message.user("Child"),
            created_at="2026-10-20T03:00:00Z",
        )
        project_run_end(store, run_id=child.id, finished_at="2026-10-20T04:00:00Z")
        store.finish_step(
            ref=step.ref,
            kind="run",
            status="succeeded",
            output=None,
            report=None,
            error=None,
            finished_at="2026-10-20T04:00:00Z",
        )
        project_run_end(store, run_id=parent.id, finished_at="2026-10-20T05:00:00Z")
        store.create_thread(thread_id="chat_other", created_at="2026-10-20T04:30:00Z")

        history = RunHistory(store)
        detail = history.get_thread(thread)
        assert detail is not None and detail.updated_at == "2026-10-20T05:00:00Z"
        assert history.list_threads(limit=1)[0].id == thread
    finally:
        store.close()


def test_run_history_projects_fork_and_rewind_from_one_snapshot(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    ids = IdIssuer(tmp_path / "runs.db", agent_name="alice")
    manager = ThreadManager(store, ids)
    try:
        source = manager.create(kind=ThreadKind.CHAT)
        first = project_run_start(
            store,
            run_id="run_first",
            thread_id=source,
            input=Message.user("first"),
        )
        project_run_end(store, run_id=first.id)
        second = project_run_start(
            store,
            run_id="run_second",
            thread_id=source,
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
            thread_id="chat_summary",
            input=Message.user("hello"),
        )
        project_run_end(store, run_id=run.id)

        thread = RunHistory(store).get_thread(str(run.thread), run_limit=0)

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
            thread_id="chat_output",
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
            output=Output(
                value_for_type(
                    "Part",
                    FieldRef.from_path(step.ref, "output", "value", 1),
                ),
                "_",
            ),
        )

        history = RunHistory(store)
        detail = history.get_run(run.id)
        thread = history.get_thread(str(run.thread))

        assert store.run_output(run_id=run.id) == (TextPart("result"),)
        assert detail is not None
        expected = Output(
            value_for_type("Part", FieldRef.from_path(step.ref, "output", "value", 1)),
            "_",
        )
        assert detail.output == expected
        assert thread is not None
        assert thread.runs[0].output == expected
    finally:
        store.close()


@pytest.mark.parametrize("reopen", (False, True))
def test_run_history_resolves_pass_through_control_output(
    tmp_path: Path, reopen: bool
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_passthrough",
            thread_id="chat_passthrough",
            input=Message.user("unchanged"),
        )
        project_run_end(
            store,
            run_id=run.id,
            output=FieldRef.from_path(
                ControlRef.for_run(run.id, 0), "payload", "input", "_"
            ),
        )

        if reopen:
            store.close()
            store = RunStore(tmp_path / "runs.db")
        stored = store.get_run(run_id=run.id)
        detail = RunHistory(store).get_run(run.id)

        assert stored is not None
        assert stored.control == ControlRef.for_run(run.id, 0)
        assert stored.output == Output(
            value_for_type(
                "Part[]",
                FieldRef.from_path(
                    ControlRef.for_run(run.id, 0), "payload", "input", "_"
                ),
            ),
            "_",
        )
        assert store.run_output(run_id=run.id) == Message.user("unchanged").parts
        assert detail is not None
        assert detail.output == Output(
            value_for_type(
                "Part[]",
                FieldRef.from_path(
                    ControlRef.for_run(run.id, 0), "payload", "input", "_"
                ),
            ),
            "_",
        )
    finally:
        store.close()


def test_resolve_output_rejects_a_pointer_to_a_different_type(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_mismatch",
            thread_id="chat_mismatch",
            input=Message.user("not a number"),
        )

        with pytest.raises(TypeError, match="Number"):
            store.resolve_output(
                Output(
                    value_for_type(
                        "Number",
                        FieldRef.from_path(
                            ControlRef.for_run(run.id, 0), "payload", "input", "_"
                        ),
                    ),
                    "_",
                )
            )
    finally:
        store.close()


def test_run_history_keeps_rewound_records_outside_the_thread_view(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        first = project_run_start(
            store,
            run_id="run_first",
            thread_id="chat_thread",
            input=Message.user("first"),
        )
        project_run_end(store, run_id=first.id)
        second = project_run_start(
            store,
            run_id="run_second",
            thread_id="chat_thread",
            input=Message.user("second"),
        )
        project_run_end(store, run_id=second.id)
        thread = store.get_thread(thread_id="chat_thread")
        assert thread is not None
        store.rewind_thread(
            thread_id="chat_thread",
            anchor=first.id,
            request_id=None,
            expected_head=store.thread_view(thread.id).head,
            created_at="2026-01-01T00:00:05Z",
        )

        rewound = RunHistory(store).get_run(second.id)
        assert rewound is not None
        assert "ejected" not in {field.name for field in fields(rewound)}
        assert RunHistory(store).list_runs(thread_id="chat_thread") == []
    finally:
        store.close()
