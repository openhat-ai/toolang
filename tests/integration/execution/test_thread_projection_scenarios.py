"""Logical Thread views preserve physical execution records."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import sqlite3

import pytest

from tests.support.execution_fixtures import (
    project_run_end,
    project_run_start,
    project_step,
)
from toolang.base.types.message import Message, TextPart
from toolang.execution.inspection.history import RunHistory
from toolang.execution.store import RunStore
from toolang.execution.types import Pointer, StepRef


@pytest.fixture
def store(tmp_path: Path) -> Iterator[RunStore]:
    value = RunStore(tmp_path / "runs.db")
    try:
        yield value
    finally:
        value.close()


def _append(
    store: RunStore, thread: str, run: str, *, parent: StepRef | None = None
) -> None:
    project_run_start(
        store,
        run_id=run,
        thread_id=thread,
        parent=parent,
        input=Message.user(run),
        created_at="2026-01-01T00:00:00Z",
    )
    project_step(
        store,
        run_id=run,
        step_index=0,
        kind="value",
        status="succeeded",
        input=(),
        output=(TextPart(run),),
        started_at="2026-01-01T00:00:01Z",
        finished_at="2026-01-01T00:00:02Z",
    )
    project_run_end(store, run_id=run)


def _physical(store: RunStore) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(store.db_path) as connection:
        return {
            table: connection.execute(
                f"SELECT rowid, * FROM {table} ORDER BY rowid"
            ).fetchall()
            for table in ("runs", "steps")
        }


def _fork(store: RunStore, source: str, target: str, anchor: str | None = None) -> None:
    before = _physical(store)
    store.fork_thread(
        thread_id=target,
        source=source,
        anchor=anchor,
        request_id=None,
        created_at="2026-01-01T00:00:03Z",
    )
    assert _physical(store) == before


def _rewind(store: RunStore, thread: str, anchor: str) -> None:
    before = _physical(store)
    visible = store.list_thread_history_chronological(thread_id=thread)
    _thread, _control, removed = store.rewind_thread(
        thread_id=thread,
        anchor=anchor,
        request_id=None,
        expected_head=store.thread_view(thread).head,
        created_at="2026-01-01T00:00:04Z",
    )
    assert _physical(store) == before
    start = next(i for i, run in enumerate(visible) if run.id == anchor)
    assert removed == tuple(run.id for run in visible[start:])


def test_nested_forks_capture_source_views_and_closed_rewinds_survive_restart(
    store: RunStore,
) -> None:
    # Equal timestamps and deliberately unsorted IDs exercise durable insertion order.
    _append(store, "chat_source", "run_z")
    _append(store, "chat_source", "run_child", parent=StepRef.from_local("run_z", "0"))
    _append(store, "chat_source", "run_a")
    _rewind(store, "chat_source", "run_a")
    _append(store, "chat_source", "run_b")
    _fork(store, "chat_source", "chat_fork")
    _rewind(store, "chat_source", "run_z")
    _append(store, "chat_source", "run_c")
    _fork(store, "chat_fork", "chat_nested")
    _rewind(store, "chat_fork", "run_b")
    _append(store, "chat_fork", "run_d")
    _fork(store, "chat_fork", "chat_repeated")
    _rewind(store, "chat_fork", "run_z")
    _append(store, "chat_fork", "run_e")

    expected = {
        "chat_source": ["run_c"],
        "chat_fork": ["run_e"],
        "chat_nested": ["run_z", "run_child", "run_b"],
        "chat_repeated": ["run_z", "run_child", "run_d"],
    }
    reopened = RunStore(store.db_path)
    try:
        for reader in (store, reopened):
            for thread, ids in expected.items():
                assert [
                    r.id
                    for r in reader.list_thread_history_chronological(thread_id=thread)
                ] == ids
                assert [
                    r.id for r in reader.list_runs(thread_id=thread, limit=None)
                ] == ids[::-1]
                assert [
                    r.record.id for r in reader.inspect_runs(thread_id=thread)
                ] == ids[::-1]
                view = RunHistory(reader).thread_view(thread)
                assert [run.id for run in view.tree()] == ids
                assert all(view.contains(run_id) for run_id in ids)
                detail = RunHistory(reader).get_thread(thread)
                assert detail is not None
                assert [r.id for r in detail.runs] == ids
            inherited = reader.get_run(run_id="run_z")
            assert inherited is not None and str(inherited.thread) == "chat_source"
            assert (
                reader.get_record(Pointer(StepRef.from_local("run_z", "0"))) is not None
            )
            assert reader.list_thread_runs_chronological(thread_id="chat_nested") == ()
            assert [
                r.id
                for r in reader.list_thread_history_chronological(
                    thread_id="chat_fork", include_rewound=True
                )
            ] == ["run_z", "run_child", "run_b", "run_d", "run_e"]
            assert [r.id for r in reader.thread_view("chat_nested").runs()] == [
                "run_z",
                "run_b",
            ]
    finally:
        reopened.close()


@pytest.mark.parametrize("active_child", (False, True))
def test_fork_validates_every_run_in_the_prefix_tree(
    store: RunStore, active_child: bool
) -> None:
    _append(store, "chat_source", "run_first")
    if active_child:
        project_run_start(
            store,
            run_id="run_child",
            thread_id="chat_source",
            input=Message.user("child"),
            parent=StepRef.from_local("run_first", "0"),
        )
    _append(store, "chat_source", "run_last")
    if not active_child:
        store._conn.execute(
            "UPDATE runs SET status='running', finished_at=NULL WHERE id='run_first'"
        )
        store._conn.commit()
    before = _physical(store)
    with pytest.raises(ValueError, match="nonterminal run"):
        _fork(store, "chat_source", "chat_fork", "run_last")
    assert store.get_thread(thread_id="chat_fork") is None
    assert store.list_thread_controls(thread_id="chat_fork") == ()
    assert _physical(store) == before


def test_stale_rewind_head_is_rejected_without_writes(store: RunStore) -> None:
    _append(store, "chat_source", "run_first")
    _append(store, "chat_source", "run_second")
    head = store.thread_view("chat_source").head
    _rewind(store, "chat_source", "run_second")
    before = _physical(store)
    with pytest.raises(ValueError, match="head changed"):
        store.rewind_thread(
            thread_id="chat_source",
            anchor="run_first",
            request_id=None,
            expected_head=head,
            created_at="2026-01-01T00:00:04Z",
        )
    assert _physical(store) == before
    assert len(store.list_thread_controls(thread_id="chat_source")) == 2
