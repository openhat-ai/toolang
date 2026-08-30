"""Focused historical ownership inspection tests."""

from __future__ import annotations

from pathlib import Path

from toolang.base.types.message import Message, TextPart
from toolang.execution.store import RunStore
from toolang.execution.types import (
    IterationOccurrence,
    Occurrence,
    OccurrencePosition,
)
from tests.support.execution_fixtures import (
    project_run_control,
    project_run_end,
    project_run_start,
    project_step,
)


def test_focused_relations_preserve_run_and_step_ownership(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        root = project_run_start(
            store,
            run_id="run_inspection_root",
            thread_id="term_inspection",
            origin="test",
            input=Message.user("Inspect"),
            runnable_kind="flow",
            runnable_name="root",
        )
        run_step = project_step(
            store,
            run_id=root.id,
            step_index=0,
            kind="run",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        loop = project_step(
            store,
            run_id=root.id,
            step_index=1,
            kind="loop",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        nested = project_step(
            store,
            parent=loop.path,
            index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart("nested"),),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        child = project_run_start(
            store,
            run_id="run_inspection_child",
            thread_id=root.thread,
            origin="test",
            input=Message.user("Child"),
            parent=run_step.path,
            runnable_name="child",
        )
        child_step = project_step(
            store,
            run_id=child.id,
            step_index=0,
            kind="model",
            status="succeeded",
            input=(),
            output=(TextPart("child"),),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        project_run_end(store, run_id=child.id)
        project_run_end(store, run_id=root.id)

        runs = store.inspect_runs(thread_id=root.thread)
        root_steps = store.inspect_steps(run_id=root.id)
        child_runs = store.inspect_child_runs(parent=run_step)
        nested_steps = store.inspect_child_steps(parent=loop)
        child_steps = store.inspect_steps(run_id=child.id)
    finally:
        store.close()

    by_id = {item.record.id: item for item in runs}
    assert by_id[root.id].runnable == "flow:root"
    assert by_id[root.id].step_count == 3
    assert by_id[child.id].step_count == 1
    assert [item.record.path for item in root_steps] == [
        run_step.path,
        loop.path,
        nested.path,
    ]
    assert root_steps[0].child_run_count == 1
    assert root_steps[1].child_run_count == 0
    assert [item.record.id for item in child_runs] == [child.id]
    assert [item.record.path for item in nested_steps] == [nested.path]
    assert [item.record.path for item in child_steps] == [child_step.path]


def test_focused_multi_table_read_uses_one_sqlite_snapshot(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_inspection_transaction",
            thread_id="term_inspection_transaction",
            origin="test",
            input=Message.user("Transaction"),
        )
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart("value"),),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        statements: list[str] = []
        store._conn.set_trace_callback(statements.append)
        inspected = store.inspect_runs()
        store._conn.set_trace_callback(None)
    finally:
        store.close()

    assert [item.record.id for item in inspected] == [run.id]
    assert sum(statement == "BEGIN" for statement in statements) == 1
    assert sum(statement == "COMMIT" for statement in statements) == 1
    begin = statements.index("BEGIN")
    commit = statements.index("COMMIT")
    assert begin < commit
    assert all(
        statement.startswith("SELECT") for statement in statements[begin + 1 : commit]
    )


def test_structural_snapshot_uses_one_transaction_without_rowid(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_tree_transaction",
            thread_id="term_tree_transaction",
            origin="test",
            input=Message.user("Tree transaction"),
        )
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart("value"),),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        statements: list[str] = []
        store._conn.set_trace_callback(statements.append)
        snapshot = store.load_execution_snapshot(root=run.id)
        store._conn.set_trace_callback(None)
    finally:
        store.close()

    assert snapshot.root == run
    assert sum(statement == "BEGIN" for statement in statements) == 1
    assert sum(statement == "COMMIT" for statement in statements) == 1
    assert not any("rowid" in statement.lower() for statement in statements)


def test_run_inspection_reads_only_each_run_entry_control(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_entry_scope",
            thread_id="term_entry_scope",
            origin="test",
            input=Message.user("Entry scope"),
        )
        unrelated = project_run_control(
            store,
            run_id=run.id,
            kind="cancel",
            input=Message.user("Cancel"),
        )
        with store.write_transaction():
            store._conn.execute(
                'UPDATE controls SET payload = ? WHERE target = ? AND "index" = ?',
                ("{", unrelated.target, unrelated.index),
            )

        inspected = store.inspect_runs()
        snapshot = store.load_execution_snapshot(root=run.id)
    finally:
        store.close()

    assert [item.record.id for item in inspected] == [run.id]
    assert inspected[0].runnable == "agic:test"
    assert [(entry.target, entry.index) for entry in snapshot.entries] == [
        (run.id, run.control.index)
    ]


def test_direct_child_run_order_keeps_semantic_coordinates_separate(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        root = project_run_start(
            store,
            run_id="run_relation_order",
            thread_id="term_relation_order",
            origin="test",
            input=Message.user("Relation order"),
        )
        parallel = project_step(
            store,
            run_id=root.id,
            step_index=0,
            kind="par",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        loop = project_step(
            store,
            run_id=root.id,
            step_index=1,
            kind="loop",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        parallel_later_id = project_run_start(
            store,
            run_id="run_z_parallel",
            thread_id=root.thread,
            origin="test",
            input=Message.user("Z"),
            parent=parallel.path,
            created_at="2026-01-01T00:00:00Z",
            context={
                "occurrence": Occurrence(
                    item=OccurrencePosition(index=0, count=1),
                    lane=OccurrencePosition(index=0, count=1),
                )
            },
        )
        parallel_first_id = project_run_start(
            store,
            run_id="run_a_parallel",
            thread_id=root.thread,
            origin="test",
            input=Message.user("A"),
            parent=parallel.path,
            created_at="2026-01-01T00:00:01Z",
            context={
                "occurrence": Occurrence(
                    item=OccurrencePosition(index=0, count=1),
                    lane=OccurrencePosition(index=0, count=1),
                )
            },
        )
        loop_body = project_run_start(
            store,
            run_id="run_loop_body",
            thread_id=root.thread,
            origin="test",
            input=Message.user("Body"),
            parent=loop.path,
            context={
                "occurrence": Occurrence(
                    item=OccurrencePosition(index=1_000_001, count=1_000_002),
                    iteration=IterationOccurrence(index=0, phase="body"),
                )
            },
        )
        loop_until = project_run_start(
            store,
            run_id="run_loop_until",
            thread_id=root.thread,
            origin="test",
            input=Message.user("Until"),
            parent=loop.path,
            context={
                "occurrence": Occurrence(
                    item=OccurrencePosition(index=0, count=1),
                    iteration=IterationOccurrence(index=0, phase="until"),
                )
            },
        )

        parallel_children = store.inspect_child_runs(parent=parallel)
        loop_children = store.inspect_child_runs(parent=loop)
    finally:
        store.close()

    assert [item.record.id for item in parallel_children] == [
        parallel_first_id.id,
        parallel_later_id.id,
    ]
    assert [item.record.id for item in loop_children] == [
        loop_body.id,
        loop_until.id,
    ]
