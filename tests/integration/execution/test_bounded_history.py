"""Bounded reads preserve captured facts without duplicating execution records."""

from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from tests.support.execution_fixtures import (
    accept_run,
    project_run_control,
    project_run_end,
    project_run_start,
    project_step,
)
from toolang.base.types.message import Message, TextPart, ToolCallPart, ToolResultPart
from toolang.base.types.run import ModelCall
from toolang.execution.errors import HistoryChangedError
from toolang.execution.history import RunHistory
from toolang.execution.records import RunControlPayload, RunRecord, StepRecord
from toolang.execution.run_view import RunView
from toolang.execution.store import RunStore
from toolang.execution.thread_view import ThreadView
from toolang.execution.values import parts_from_local
from toolang.execution.types import (
    Output,
    ControlRef,
    FieldRef,
    Local,
    ModelStepGiven,
    RunRef,
    StepKind,
    StepRef,
    ThreadRef,
)
from toolang.lang.input import RunnableInput
import toolang.execution.store as store_module


@pytest.fixture
def store(tmp_path: Path) -> Iterator[RunStore]:
    value = RunStore(tmp_path / "runs.db")
    try:
        yield value
    finally:
        value.close()


def start(
    store: RunStore,
    name: str = "run_a",
    *,
    thread: str = "term_a",
    parent: StepRef | None = None,
) -> RunRecord:
    return project_run_start(
        store,
        run_id=name,
        thread_id=thread,
        parent=parent,
        origin="chat",
        input=Message.user(name),
    )


def step(
    store: RunStore,
    run: str = "run_a",
    index: int = 0,
    *,
    kind: StepKind = "value",
    output: str = "value",
) -> StepRecord:
    assert kind in {"model", "value", "tool"}
    return project_step(
        store,
        run_id=run,
        step_index=index,
        kind=kind,
        status="succeeded",
        input=(),
        output=(TextPart(output),),
        started_at="2026-01-01T00:00:01Z",
        finished_at="2026-01-01T00:00:02Z",
    )


def retry(store: RunStore, run: str) -> None:
    control = store.get_run_control(run_id=run, index=0)
    assert control is not None and isinstance(control.payload, RunControlPayload)
    payload = control.payload
    store.accept_retry(
        run_id=run,
        anchor=None,
        resources=payload.resources,
        limits=payload.limits,
        state=payload.state,
        sandbox="host",
        request_id=None,
        created_at="2026-01-02T00:00:00Z",
    )


def pages(
    history: RunHistory, first: RunView | ThreadView
) -> list[RunView | ThreadView]:
    result = [first]
    while result[-1].cursor is not None:
        result.append(history.next_page(result[-1].cursor))
    return result


@pytest.mark.parametrize("query", ["run", "child", "runs", "thread"])
def test_inspection_read_keeps_one_snapshot_during_retry(
    store: RunStore, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    start(store)
    parent = step(store)
    start(store, "run_child", parent=parent.ref)
    step(store, "run_child")
    project_run_end(store, run_id="run_child")
    project_run_end(store, run_id="run_a")
    history = RunHistory(store)
    read = {
        "run": lambda: history.get_run("run_a"),
        "child": lambda: history.get_run("run_child"),
        "runs": lambda: history.list_runs(thread_id="term_a"),
        "thread": lambda: history.get_thread("term_a"),
    }[query]
    expected = read()
    method = "list_steps" if query in {"run", "child"} else "list_steps_for_runs"
    original = getattr(store, method)
    writer = RunStore(store.db_path)
    try:

        def interleave(**kwargs: object) -> object:
            retry(writer, "run_a")
            return original(**kwargs)

        monkeypatch.setattr(store, method, interleave)
        assert read() == expected
        assert writer.get_run(run_id="run_child") is None
    finally:
        writer.close()


def test_thread_list_keeps_one_snapshot_during_rewind(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("run_a", "run_b"):
        start(store, name)
        project_run_end(store, run_id=name)
    history = RunHistory(store)
    expected = history.list_threads()
    head = store.thread_view("term_a").head
    original = store.thread_views
    writer = RunStore(store.db_path)
    try:

        def interleave(thread_ids: Sequence[str]) -> dict[str, ThreadView]:
            writer.rewind_thread(
                thread_id="term_a",
                anchor="run_b",
                expected_head=head,
                request_id=None,
                created_at="2026-01-02T00:00:00Z",
            )
            return original(thread_ids)

        monkeypatch.setattr(store, "thread_views", interleave)
        assert history.list_threads() == expected
        assert len(writer.thread_view("term_a").runs()) == 1
    finally:
        writer.close()


def test_thread_page_keeps_membership_across_append_rewind_and_restart(
    store: RunStore,
) -> None:
    for name in ("run_z", "run_a", "run_b"):
        start(store, name)
        project_run_end(store, run_id=name)
    history = RunHistory(store)
    first = history.thread_view("term_a", limit=1)
    assert [r.id for r in first.runs()] == ["run_z"]
    captured_thread = first.record
    writer = RunStore(store.db_path)
    try:
        writer.rewind_thread(
            thread_id="term_a",
            anchor="run_a",
            request_id=None,
            expected_head=first.head,
            created_at="2026-01-02T00:00:00Z",
        )
        start(writer, "run_c")
        project_run_end(writer, run_id="run_c")
    finally:
        writer.close()
    reopened = RunStore(store.db_path, read_only=True)
    try:
        captured = pages(RunHistory(reopened), first)
        assert all(
            isinstance(page, ThreadView) and page.record == captured_thread
            for page in captured
        )
        assert [
            run.id
            for page in captured
            if isinstance(page, ThreadView)
            for run in page.runs()
        ] == ["run_z", "run_a", "run_b"]
        assert [
            run.id for run in RunHistory(reopened).thread_view("term_a").runs()
        ] == ["run_z", "run_c"]
    finally:
        reopened.close()


def test_thread_ranges_reverse_pages_and_inherited_children(store: RunStore) -> None:
    start(store)
    parent = step(store)
    start(store, "run_child", parent=parent.ref)
    project_run_end(store, run_id="run_child")
    project_run_end(store, run_id="run_a")
    start(store, "run_b")
    project_run_end(store, run_id="run_b")
    store.fork_thread(
        thread_id="term_fork",
        source="term_a",
        anchor=None,
        request_id=None,
        created_at="2026-01-02T00:00:00Z",
    )
    start(store, "run_c", thread="term_fork")
    project_run_end(store, run_id="run_c")
    history = RunHistory(store)
    view = history.thread_view("term_fork", end=RunRef("run_c"))
    assert [run.id for run in view.runs()] == ["run_a", "run_b"]
    assert view.contains("run_child") and not view.contains("run_c")
    assert [run.thread for run in view.tree()] == [ThreadRef("term_a")] * 3
    reversed_pages = pages(
        history, history.thread_view("term_fork", limit=2, reverse=True)
    )
    assert [
        [run.id for run in page.runs()]
        for page in reversed_pages
        if isinstance(page, ThreadView)
    ] == [["run_b", "run_c"], ["run_a"]]
    assert [
        run.id for run in history.thread_view("term_fork", begin=RunRef("run_b")).runs()
    ] == ["run_b", "run_c"]


def test_thread_page_does_not_decode_unselected_run_outputs(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(8):
        run = start(store, f"run_{index}")
        project_run_end(
            store, run_id=run.id, output=Output(Local("large output " * 1000), None)
        )
    original = store_module._run_from_row
    decoded: list[str] = []

    def decode(row: sqlite3.Row) -> RunRecord:
        decoded.append(row["id"])
        return original(row)

    monkeypatch.setattr(store_module, "_run_from_row", decode)
    first = RunHistory(store).thread_view("term_a", limit=1)
    assert decoded == ["run_0"]
    assert first.cursor is not None and "large output" not in first.cursor


def test_run_pages_capture_numeric_order_without_decoding_all_steps(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    start(store)
    for index in (10, 2, 0):
        step(store, index=index)
    history = RunHistory(store)
    original = store_module._step_from_row
    decoded: list[str] = []

    def decode(row: sqlite3.Row) -> StepRecord:
        decoded.append(row["id"])
        return original(row)

    monkeypatch.setattr(store_module, "_step_from_row", decode)
    first = history.run_view("run_a", limit=1)
    assert decoded == ["run_a.0"]
    assert [c.id for c in first.dependencies] == ["run_a@0"]
    step(store, index=11)
    captured = pages(history, first)
    assert [
        item.id
        for page in captured
        if isinstance(page, RunView)
        for item in page.entries
    ] == ["run_a.0", "run_a.2", "run_a.10", "run_a@0"]
    assert [
        s.id
        for s in history.run_view(
            "run_a", begin=StepRef.parse("run_a.2"), end=StepRef.parse("run_a.11")
        ).steps()
    ] == ["run_a.2", "run_a.10"]


def test_step_range_excludes_unrelated_controls(store: RunStore) -> None:
    start(store)
    step(store)
    step(store, index=1)
    late = project_run_control(
        store, run_id="run_a", kind="steer", input=Message.user("later input")
    )
    store.begin_step(
        ref=StepRef.parse("run_a.2"),
        kind="model",
        input=(),
        given=ModelStepGiven("test", ModelCall("", [])),
        preceded_by=(late.ref,),
        started_at="2026-01-01T00:00:03Z",
    )
    history = RunHistory(store)
    first = history.run_view("run_a", end=StepRef.parse("run_a.2"), limit=1)
    assert first.cursor is not None
    store.finish_run_controls(run_id="run_a", indexes=(late.index,), finished_at="4")
    captured = pages(history, first)
    assert [
        record.id
        for page in captured
        if isinstance(page, RunView)
        for record in page.entries
    ] == [
        "run_a.0",
        "run_a.1",
    ]
    assert all(
        late.id not in {control.id for control in page.dependencies}
        for page in captured
        if isinstance(page, RunView)
    )
    selected = history.run_view("run_a", begin=StepRef.parse("run_a.2"))
    assert selected.controls() == ()
    assert late.id in {control.id for control in selected.dependencies}


def test_empty_step_range_has_no_controls(store: RunStore) -> None:
    start(store)
    boundary = step(store).ref
    view = RunHistory(store).run_view("run_a", begin=boundary, end=boundary)
    assert view.entries == view.dependencies == ()
    assert view.cursor is None


@pytest.mark.parametrize("thread_page", [False, True])
def test_retry_invalidates_pages_even_when_step_ids_are_reused(
    store: RunStore, thread_page: bool
) -> None:
    start(store)
    step(store)
    step(store, index=1)
    project_run_end(store, run_id="run_a")
    start(store, "run_b")
    project_run_end(store, run_id="run_b")
    history = RunHistory(store)
    first = (
        history.thread_view("term_a", limit=1)
        if thread_page
        else history.run_view("run_a", limit=1)
    )
    assert first.cursor is not None
    retry(store, "run_a")
    step(store, output="replacement")
    with pytest.raises(HistoryChangedError, match="changed|missing"):
        history.next_page(first.cursor)


def test_run_page_is_restartable_and_invalidates_changed_control_dependencies(
    store: RunStore,
) -> None:
    start(store)
    step(store)
    step(store, index=1)
    pending = project_run_control(
        store, run_id="run_a", kind="steer", input=Message.user("pending")
    )
    history = RunHistory(store)
    first = history.run_view("run_a", limit=1)
    assert first.cursor is not None
    reopened = RunStore(store.db_path, read_only=True)
    try:
        assert isinstance(RunHistory(reopened).next_page(first.cursor), RunView)
        store.finish_run_controls(
            run_id="run_a", indexes=(pending.index,), finished_at="2026-01-02T00:00:00Z"
        )
        with pytest.raises(HistoryChangedError, match=pending.id):
            RunHistory(reopened).next_page(first.cursor)
    finally:
        reopened.close()


def test_run_page_rejects_completion_of_a_captured_running_step(
    store: RunStore,
) -> None:
    start(store)
    model = store.begin_step(
        ref=StepRef.parse("run_a.0"),
        kind="model",
        input=(),
        given=ModelStepGiven("test", ModelCall("", [])),
        started_at="2026-01-01T00:00:00Z",
    )
    history = RunHistory(store)
    first = history.run_view("run_a", limit=1)
    assert first.cursor is not None
    store.finish_step(
        ref=model.ref,
        kind="model",
        status="canceled",
        output=Output(Local("partial"), None),
        noted=None,
        error=None,
        finished_at="2026-01-01T00:00:01Z",
    )
    with pytest.raises(HistoryChangedError, match=model.id):
        history.next_page(first.cursor)


def test_run_view_keeps_control_relationships_separate_from_input(
    store: RunStore,
) -> None:
    start(store)
    steer = project_run_control(
        store, run_id="run_a", kind="steer", input=Message.user("adjust")
    )
    store.finish_run_controls(
        run_id="run_a", indexes=(steer.index,), finished_at="2026-01-01T00:00:00Z"
    )
    control_input = FieldRef.from_path(steer.ref, "payload", "input")
    first = store.begin_step(
        ref=StepRef.parse("run_a.0"),
        kind="model",
        input=(control_input,),
        given=ModelStepGiven("test", ModelCall("", [])),
        preceded_by=(ControlRef.for_run("run_a", 0),),
        started_at="2026-01-01T00:00:01Z",
    )
    store.finish_step(
        ref=first.ref,
        kind="model",
        status="canceled",
        output=Output(Local("partial"), None),
        noted=None,
        error=None,
        aborted_by=steer.ref,
        finished_at="2026-01-01T00:00:02Z",
    )
    store.begin_step(
        ref=StepRef.parse("run_a.1"),
        kind="model",
        input=(),
        given=ModelStepGiven("test", ModelCall("", [])),
        preceded_by=(steer.ref,),
        started_at="2026-01-01T00:00:03Z",
    )
    view = RunHistory(store).run_view("run_a")
    assert [
        (event.step.local, event.phase, event.controls) for event in view.timeline()
    ] == [
        ("0", "begin", (ControlRef.for_run("run_a", 0),)),
        ("0", "end", (steer.ref,)),
        ("1", "begin", (steer.ref,)),
    ]
    assert view.steps()[0].input == (control_input,)
    assert view.steps()[0].output == Output(Local("partial"), None)
    assert [c.ref for c in view.controls()].count(steer.ref) == 1


def test_raw_pages_may_cross_tool_exchange_without_losing_parts(
    store: RunStore,
) -> None:
    start(store)
    call = ToolCallPart("call", "test", "test", {})
    result = ToolResultPart("call", "test", "test", {"value": "done"})
    for index, kind, output in ((0, "model", call), (1, "tool", result)):
        project_step(
            store,
            run_id="run_a",
            step_index=index,
            kind=kind,
            status="succeeded",
            input=(),
            output=(output,),
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:01Z",
        )
    history = RunHistory(store)
    captured = pages(history, history.run_view("run_a", limit=1))
    assert [
        parts_from_local(s.output.local)
        for page in captured
        if isinstance(page, RunView)
        for s in page.steps()
        if s.output is not None
    ] == [(call,), (result,)]


def test_child_view_excludes_parent_and_sibling_steps_but_loads_state_control(
    store: RunStore,
) -> None:
    start(store)
    parent = step(store)
    start(store, "run_child", parent=parent.ref)
    step(store, "run_child")
    start(store, "run_sibling", parent=parent.ref)
    step(store, "run_sibling")
    history = RunHistory(store)
    child = history.run_view("run_child")
    assert [s.id for s in child.steps()] == ["run_child.0"]
    assert any(
        control.ref == child.steps()[0].state
        for control in (*child.controls(), *child.dependencies)
    )
    assert [s.id for s in history.run_view("run_a").steps()] == [parent.id]


def test_child_pages_do_not_decode_ancestor_outputs(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    start(store)
    parent = step(store)
    start(store, "run_child", parent=parent.ref)
    child_step = step(store, "run_child")
    start(store, "run_leaf", parent=child_step.ref)
    step(store, "run_leaf")
    project_run_end(store, run_id="run_leaf")
    for name in ("run_child", "run_a"):
        project_run_end(
            store, run_id=name, output=Output(Local("large output " * 1000), None)
        )
    original = store_module._run_from_row

    def decode(row: sqlite3.Row) -> RunRecord:
        assert row["id"] == "run_leaf", "ancestor checks must not load outputs"
        return original(row)

    monkeypatch.setattr(store_module, "_run_from_row", decode)
    history = RunHistory(store)
    captured = pages(history, history.run_view("run_leaf", limit=1))
    assert [
        s.id for page in captured if isinstance(page, RunView) for s in page.steps()
    ] == ["run_leaf.0"]


def test_output_and_model_call_reads_are_independent(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    start(store)
    call = ModelCall("protocol", [Message.user("exact call")])
    model = store.begin_step(
        ref=StepRef.parse("run_a.0"),
        kind="model",
        input=(),
        given=ModelStepGiven("test", call),
        started_at="2026-01-01T00:00:00Z",
    )
    store.finish_step(
        ref=model.ref,
        kind="model",
        status="succeeded",
        output=Output(Local("answer"), None),
        noted=None,
        error=None,
        finished_at="2026-01-01T00:00:01Z",
    )
    project_run_end(
        store,
        run_id="run_a",
        output=Output(
            Local.typed(
                "Text", FieldRef.from_path(model.ref, "output", "local", "value")
            ),
            None,
        ),
    )
    history = RunHistory(store)
    assert history.get_model_call(model.ref) == call
    monkeypatch.setattr(
        store,
        "rebuild_model_calls",
        lambda *_args, **_kwargs: pytest.fail("output must not rebuild calls"),
    )
    monkeypatch.setattr(
        store,
        "list_steps",
        lambda **_kwargs: pytest.fail("output must not load all steps"),
    )
    assert history.get_output("run_a") == Output(Local("answer"), None)
    with pytest.raises(KeyError):
        history.get_output("run_missing")
    start(store, "run_empty")
    assert history.get_output("run_empty") is None


def test_compaction_output_reader_uses_latest_success_and_keeps_range_metadata(
    store: RunStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start(store)
    start(store, "run_b")
    history = RunHistory(store)
    assert history.get_compaction("term_a") is None
    start(store, "run_old", thread="compact_term_a")
    project_run_end(
        store, run_id="run_old", output=Output(Local("obsolete summary"), None)
    )
    output = Output(
        Local(
            {
                "thread": "term_a",
                "begin": None,
                "end": "run_b",
                "summary": "earlier facts",
            }
        ),
        None,
    )
    accept_run(
        store,
        run_id="run_compact",
        parent=None,
        thread="compact_term_a",
        input=RunnableInput({"thread": "term_a", "end": "run_b"}),
        context={},
        request_id=None,
        created_at="2026-01-01T00:00:00Z",
    )
    project_run_end(store, run_id="run_compact", output=output)
    start(store, "run_failed", thread="compact_term_a")
    project_run_end(store, run_id="run_failed", status="failed")
    original = store_module._run_from_row

    def decode(row: sqlite3.Row) -> RunRecord:
        assert row["id"] != "run_old", "compact lookup must not load older summaries"
        return original(row)

    monkeypatch.setattr(store_module, "_run_from_row", decode)
    found = history.get_compaction("term_a")
    assert found is not None
    assert found.ref == FieldRef.from_path(RunRef("run_compact"), "output")
    assert found.output == output
    with pytest.raises(KeyError):
        history.get_compaction("term_missing")


def test_history_reads_do_not_modify_durable_records(store: RunStore) -> None:
    start(store)
    step(store)
    project_run_end(store, run_id="run_a")

    def dump() -> str:
        with sqlite3.connect(store.db_path) as db:
            return "\n".join(db.iterdump())

    before = dump()
    history = RunHistory(store)
    pages(history, history.run_view("run_a", limit=1))
    pages(history, history.thread_view("term_a", limit=1))
    assert dump() == before


def test_invalid_bounds_and_limits_are_rejected(store: RunStore) -> None:
    start(store)
    step(store)
    step(store, index=1)
    history = RunHistory(store)
    for limit in (0, -1):
        with pytest.raises(ValueError):
            history.run_view("run_a", limit=limit)
        with pytest.raises(ValueError):
            history.thread_view("term_a", limit=limit)
    with pytest.raises(ValueError):
        history.run_view("run_a", begin=StepRef.parse("run_b.0"))
    with pytest.raises(ValueError):
        history.run_view(
            "run_a", begin=StepRef.parse("run_a.1"), end=StepRef.parse("run_a.0")
        )
    with pytest.raises(ValueError):
        history.thread_view("term_a", end=RunRef("run_missing"))
    with pytest.raises(ValueError):
        history.next_page("not a cursor")


def test_reverse_run_pages_concatenate_to_the_same_records(store: RunStore) -> None:
    start(store)
    for index in range(5):
        step(store, index=index)
    history = RunHistory(store)
    reverse = pages(history, history.run_view("run_a", limit=2, reverse=True))
    assert [
        record
        for page in reversed(reverse)
        if isinstance(page, RunView)
        for record in page.entries
    ] == list(history.run_view("run_a").entries)


def test_raw_controls_preserve_all_statuses_without_manufacturing_timeline_events(
    store: RunStore,
) -> None:
    start(store)
    controls = [
        project_run_control(
            store, run_id="run_a", kind="steer", input=Message.user(str(index))
        )
        for index in range(4)
    ]
    store.finish_run_controls(
        run_id="run_a", indexes=(controls[0].index,), finished_at="1"
    )
    store.fail_run_controls(
        run_id="run_a", indexes=(controls[1].index,), error="unused", finished_at="2"
    )
    store.cancel_run_control(run_id="run_a", index=controls[2].index, canceled_at="3")
    history = RunHistory(store)
    captured = pages(history, history.run_view("run_a", limit=1))
    statuses = [
        control.status
        for page in captured
        if isinstance(page, RunView)
        for control in page.controls()
        if control.index
    ]
    assert statuses == ["applied", "wontapply", "revoked", "pending"]
    assert all(page.timeline() == () for page in captured if isinstance(page, RunView))


def test_parent_retry_invalidates_a_child_cursor(store: RunStore) -> None:
    start(store)
    parent = step(store)
    start(store, "run_child", parent=parent.ref)
    step(store, "run_child")
    project_run_end(store, run_id="run_child")
    project_run_end(store, run_id="run_a")
    history = RunHistory(store)
    child = history.run_view("run_child", limit=1)
    assert child.cursor is not None
    retry(store, "run_a")
    with pytest.raises(HistoryChangedError, match="missing|changed"):
        history.next_page(child.cursor)


def test_thread_view_keeps_physical_member_order_for_interleaved_child_records(
    store: RunStore,
) -> None:
    start(store)
    parent = step(store)
    start(store, "run_b")
    start(store, "run_child", parent=parent.ref)
    view = RunHistory(store).thread_view("term_a")
    assert [run.id for run in view.tree()] == ["run_a", "run_b", "run_child"]
    assert view.tree() == store.thread_view("term_a").tree()


def test_timeline_nests_container_end_after_its_child_boundaries(
    store: RunStore,
) -> None:
    start(store)
    parent = project_step(
        store,
        run_id="run_a",
        step_index=2,
        kind="loop",
        status="succeeded",
        input=(),
        output=(),
        started_at="0",
        finished_at="3",
    )
    child = project_step(
        store,
        parent=parent.ref,
        index=10,
        kind="value",
        status="succeeded",
        input=(),
        output=(),
        started_at="1",
        finished_at="2",
    )
    view = RunHistory(store).run_view("run_a")
    assert [(event.step, event.phase) for event in view.timeline()] == [
        (parent.ref, "begin"),
        (child.ref, "begin"),
        (child.ref, "end"),
        (parent.ref, "end"),
    ]
    assert replace(view, cursor="irrelevant").timeline() == view.timeline()
