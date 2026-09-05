"""Historical execution tree construction tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from toolang.base.types.message import Message, TextPart
from toolang.execution.inspection import ExecutionSnapshot
from toolang.execution.store import RunStore
from toolang.execution.trees import build_execution_tree, tree_to_data
from toolang.execution.types import (
    ControlRef,
    ErrorRef,
    FieldRef,
    IterationOccurrence,
    ModelAccounting,
    ModelCost,
    ModelStepNoted,
    ModelUsageMeter,
    Occurrence,
    OccurrencePosition,
    StepRef,
)
from toolang.lang.ast import RunStmt, Span
from tests.support.execution_fixtures import (
    project_run_end,
    project_run_start,
    project_step,
)


def test_tree_orders_parallel_runs_by_item_and_keeps_lane(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        root = project_run_start(
            store,
            run_id="run_parallel_root",
            thread_id="term_parallel",
            origin="test",
            input=Message.user("Parallel"),
        )
        parent = project_step(
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
        second = project_run_start(
            store,
            run_id="run_parallel_second",
            thread_id=root.thread,
            origin="test",
            input=Message.user("Second"),
            parent=parent.ref,
            context={
                "occurrence": Occurrence(
                    item=OccurrencePosition(index=1, count=2),
                    lane=OccurrencePosition(index=0, count=2),
                )
            },
        )
        first = project_run_start(
            store,
            run_id="run_parallel_first",
            thread_id=root.thread,
            origin="test",
            input=Message.user("First"),
            parent=parent.ref,
            context={
                "occurrence": Occurrence(
                    item=OccurrencePosition(index=0, count=2),
                    lane=OccurrencePosition(index=1, count=2),
                )
            },
        )
        project_run_end(store, run_id=second.id)
        project_run_end(store, run_id=first.id)
        project_run_end(store, run_id=root.id)
        tree = build_execution_tree(store.load_execution_snapshot(root=root.id))
    finally:
        store.close()

    data = tree_to_data(tree)
    assert [item["pointer"] for item in data] == [
        root.id,
        str(parent.ref),
        first.id,
        second.id,
    ]
    assert data[2]["occur"] == {
        "item": {"index": 0, "count": 2},
        "lane": {"index": 1, "count": 2},
        "iteration": None,
    }


def test_tree_merges_loop_steps_and_runs_by_iteration_and_phase(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        root = project_run_start(
            store,
            run_id="run_loop_root",
            thread_id="term_loop",
            origin="test",
            input=Message.user("Loop"),
        )
        loop = project_step(
            store,
            run_id=root.id,
            step_index=0,
            kind="loop",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:03Z",
        )
        until_run = project_run_start(
            store,
            run_id="run_loop_until",
            thread_id=root.thread,
            origin="test",
            input=Message.user("Until"),
            parent=loop.ref,
            context={
                "occurrence": Occurrence(
                    iteration=IterationOccurrence(index=0, count=1, phase="until")
                )
            },
        )
        body = project_step(
            store,
            parent=loop.ref,
            index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart("body"),),
            context={
                "occurrence": Occurrence(
                    iteration=IterationOccurrence(index=0, count=1, phase="body")
                )
            },
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        project_run_end(store, run_id=until_run.id)
        project_run_end(store, run_id=root.id)
        tree = build_execution_tree(store.load_execution_snapshot(root=root.id))
    finally:
        store.close()

    assert [node.pointer for node in tree.nodes] == [
        root.id,
        str(loop.ref),
        str(body.ref),
        until_run.id,
    ]


def test_tree_rejects_missing_parallel_coordinates_and_multiple_run_children(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        root = project_run_start(
            store,
            run_id="run_corrupt_root",
            thread_id="term_corrupt",
            origin="test",
            input=Message.user("Corrupt"),
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
        project_run_start(
            store,
            run_id="run_corrupt_parallel",
            thread_id=root.thread,
            origin="test",
            input=Message.user("Missing lane"),
            parent=parallel.ref,
            context={
                "occurrence": Occurrence(item=OccurrencePosition(index=0, count=1))
            },
        )
        snapshot = store.load_execution_snapshot(root=root.id)
        primitive_children = store.inspect_child_runs(parent=parallel)
    finally:
        store.close()

    assert [item.record.id for item in primitive_children] == ["run_corrupt_parallel"]
    with pytest.raises(ValueError, match="item and lane occurrence"):
        build_execution_tree(snapshot)

    run_step = replace(
        parallel,
        kind="run",
        given=RunStmt(span=Span(line=1), runnable="agic:test"),
    )
    child = next(run for run in snapshot.runs if run.id == "run_corrupt_parallel")
    duplicate = replace(
        child,
        id="run_corrupt_duplicate",
        control=ControlRef.for_run("run_corrupt_duplicate", 0),
    )
    child_entry = next(
        entry for entry in snapshot.entries if entry.target == child.control.target
    )
    corrupted = ExecutionSnapshot(
        root=snapshot.root,
        runs=(*snapshot.runs, duplicate),
        steps=(run_step,),
        entries=(
            *snapshot.entries,
            replace(child_entry, id=str(duplicate.control)),
        ),
    )
    with pytest.raises(ValueError, match="multiple child Runs"):
        build_execution_tree(corrupted)


def test_tree_aggregates_partial_accounting_without_inventing_values(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        root = project_run_start(
            store,
            run_id="run_metrics_root",
            thread_id="term_metrics",
            origin="test",
            input=Message.user("Metrics"),
        )
        first = project_step(
            store,
            run_id=root.id,
            step_index=0,
            kind="model",
            status="succeeded",
            input=(),
            output=(TextPart("Known"),),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        project_step(
            store,
            run_id=root.id,
            step_index=1,
            kind="model",
            status="running",
            input=(),
            output=None,
            started_at="2026-01-01T00:00:01Z",
            finished_at=None,
        )
        snapshot = store.load_execution_snapshot(root=root.id)
    finally:
        store.close()

    accounting = ModelAccounting(
        input_tokens=10,
        output_tokens=4,
        meters=(
            ModelUsageMeter(
                name="output.reasoning",
                quantity="3",
                unit="token",
            ),
        ),
        estimate=ModelCost(amount="0.025", currency="USD", complete=True),
        selected="estimated",
    )
    known = replace(
        first,
        noted=ModelStepNoted(accounting=accounting),
    )
    snapshot = replace(
        snapshot,
        steps=tuple(
            known if step.ref == first.ref else step for step in snapshot.steps
        ),
    )
    metrics = tree_to_data(build_execution_tree(snapshot))[0]["metrics"]

    assert metrics == {
        "runs": 0,
        "model_calls": 2,
        "tool_calls": 0,
        "input_tokens": 10,
        "output_tokens": 4,
        "reasoning_tokens": 3,
        "usage_complete": False,
        "reasoning_complete": False,
        "cost_usd": "0.025",
        "cost_complete": False,
        "cost_approximate": True,
    }


def test_tree_aggregates_exact_reasoning_through_nested_runs(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        root = project_run_start(
            store,
            run_id="run_reasoning_root",
            thread_id="term_reasoning",
            origin="test",
            input=Message.user("Reasoning"),
        )
        parent = project_step(
            store,
            run_id=root.id,
            step_index=0,
            kind="run",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        child = project_run_start(
            store,
            run_id="run_reasoning_child",
            thread_id=root.thread,
            origin="test",
            input=Message.user("Child"),
            parent=parent.ref,
        )
        model = project_step(
            store,
            run_id=child.id,
            step_index=0,
            kind="model",
            status="succeeded",
            input=(),
            output=(TextPart("Done"),),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        project_run_end(store, run_id=child.id)
        project_run_end(store, run_id=root.id)
        snapshot = store.load_execution_snapshot(root=root.id)
    finally:
        store.close()

    accounting = ModelAccounting(
        input_tokens=10,
        output_tokens=4,
        meters=(ModelUsageMeter("output.reasoning", "3", "token"),),
    )
    snapshot = replace(
        snapshot,
        steps=tuple(
            replace(step, noted=ModelStepNoted(accounting=accounting))
            if step.ref == model.ref
            else step
            for step in snapshot.steps
        ),
    )

    tree = build_execution_tree(snapshot)
    metrics = {
        node.pointer: node.metrics
        for node in tree.nodes
        if node.pointer in {root.id, str(parent.ref), child.id, str(model.ref)}
    }

    assert all(item.reasoning_tokens == 3 for item in metrics.values())
    assert all(item.reasoning_complete is True for item in metrics.values())


def test_step_root_snapshot_ignores_external_parent_but_rejects_internal_orphan(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        root = project_run_start(
            store,
            run_id="run_step_root",
            thread_id="term_step_root",
            origin="test",
            input=Message.user("Step root"),
        )
        loop = project_step(
            store,
            run_id=root.id,
            step_index=0,
            kind="loop",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        child = project_step(
            store,
            parent=loop.ref,
            index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(TextPart("child"),),
            context={
                "occurrence": Occurrence(
                    iteration=IterationOccurrence(index=0, count=1, phase="body")
                )
            },
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        snapshot = store.load_execution_snapshot(root=loop.ref)
    finally:
        store.close()

    tree = build_execution_tree(snapshot)
    assert [node.pointer for node in tree.nodes] == [str(loop.ref), str(child.ref)]

    orphan = replace(child, id=str(StepRef.from_local(root.id, (0, 1, 0))))
    corrupted = replace(snapshot, steps=(loop, orphan))
    with pytest.raises(ValueError, match="orphan or cycle"):
        build_execution_tree(corrupted)


def test_tree_keeps_canonical_error_pointers_and_resolves_only_its_snapshot(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        root = project_run_start(
            store,
            run_id="run_tree_errors",
            thread_id="term_tree_errors",
            origin="test",
            input=Message.user("Errors"),
        )
        first = project_step(
            store,
            run_id=root.id,
            step_index=0,
            kind="value",
            status="failed",
            input=(),
            output=None,
            error="first",
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
        second = project_step(
            store,
            run_id=root.id,
            step_index=1,
            kind="value",
            status="failed",
            input=(),
            output=None,
            error="second",
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        snapshot = store.load_execution_snapshot(root=root.id)
    finally:
        store.close()

    first_pointer = FieldRef.from_path(second.ref, "error")
    second_pointer = FieldRef.from_path(first.ref, "error")
    snapshot = replace(
        snapshot,
        steps=(
            replace(first, error=ErrorRef(first_pointer)),
            replace(second, error=ErrorRef(second_pointer)),
        ),
    )
    tree = build_execution_tree(snapshot)
    first_node = next(node for node in tree.nodes if node.pointer == str(first.ref))

    assert tree_to_data(tree)[1]["error"] == {
        "type": "ref",
        "ref": str(first_pointer),
    }
    assert tree.resolve_error(first_node.error) == (
        f"{first_pointer} (unresolved cycle)"
    )
    missing = ErrorRef(FieldRef.from_path(StepRef.from_local(root.id, (9,)), "error"))
    assert tree.resolve_error(missing) == f"{missing.ref} (unresolved)"
