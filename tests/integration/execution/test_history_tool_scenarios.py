"""History tools expose bounded execution facts without rebuilding model calls."""

import asyncio
from contextlib import closing
import threading

import pytest

from tests.support.execution_assertions import (
    assert_replayed,
    assert_run_event_integrity,
)
from tests.support.execution_fixtures import (
    project_run_start,
    project_run_end,
    project_step,
    project_run_control,
)
from tests.support.execution_harness import ExecutionHarness, RecordingRunTracer
from toolang.base.types.message import Message, TextPart, ToolCallPart, ToolResultPart
from toolang.base.types.run import ModelCall, ModelCallResult, ToolCall
from toolang.base.types.tool import ToolContext
from toolang.execution.errors import HistoryChangedError
from toolang.execution.executor.tool_history import _ToolHistory
from toolang.execution.history import RunHistory
from toolang.execution.records import RunControlPayload
from toolang.execution.schemas import record_to_data
from toolang.execution.store import RunStore
from toolang.execution.types import (
    ControlRef,
    FieldRef,
    Local,
    ModelStepGiven,
    StepRef,
    ThreadPrefix,
    ToolStepGiven,
    TypedRef,
)
from toolang.plugin.toolsets.loading import load_tools


@pytest.fixture
def store(tmp_path):
    with closing(RunStore(tmp_path / "runs.db")) as store:
        yield store


def start(store, name="run_a", thread="term_a", parent=None):
    return project_run_start(
        store,
        run_id=name,
        thread_id=thread,
        parent=parent,
        origin="chat",
        input=Message.user(name),
    )


def step(store, run="run_a", index=0, *, output=None, kind="value", status="succeeded"):
    return project_step(
        store,
        run_id=run,
        step_index=index,
        kind=kind,
        status=status,
        input=(),
        output=output if output is not None else Local("value"),
        started_at="2026-01-01T00:00:01Z",
        finished_at="2026-01-01T00:00:02Z",
    )


def read(store, tool, *, caller="term_a", **query):
    context = ToolContext(
        "run_caller",
        store.db_path.parent,
        store.db_path.parent,
        store.db_path.parent,
        history=_ToolHistory(store, caller),
    )
    return asyncio.run(load_tools()[f"history__{tool}"].invoke(query, context))


def collect(store, tool, page):
    pages = [page]
    while pages[-1]["cursor"] is not None:
        pages.append(read(store, tool, cursor=pages[-1]["cursor"]))
    return pages


def test_thread_listing_freezes_ids_but_reads_current_metadata(store):
    for identity in ("term_c", "term_b", "term_a"):
        store.create_thread(
            thread_id=identity, origin="chat", created_at="2026-01-01T00:00:00Z"
        )
    first = read(store, "read_threads", limit=1)
    assert [r["id"] for r in first["threads"]] == ["term_a"]
    store.create_thread(
        thread_id="term_new", origin="chat", created_at="2027-01-01T00:00:00Z"
    )
    start(store, thread="term_c")  # Moves C to the front of new reads, not this cursor.
    current = store.get_thread(thread_id="term_c")
    with closing(RunStore(store.db_path, read_only=True)) as reopened:
        pages = collect(reopened, "read_threads", first)
        assert [r["id"] for p in pages for r in p["threads"]] == [
            "term_a",
            "term_b",
            "term_c",
        ]
        assert pages[-1]["threads"] == [record_to_data(current)]
    assert [r["id"] for r in read(store, "read_threads")["threads"]] == list(
        store.history_thread_ids()
    )


def test_empty_thread_listing(store):
    assert read(store, "read_threads") == {"threads": [], "cursor": None}


def test_run_pages_preserve_fork_membership_across_rewind_append_and_restart(store):
    for name in ("run_c", "run_a", "run_b"):
        start(store, name)
        project_run_end(store, run_id=name)
    store.fork_thread(
        thread_id="term_fork",
        source="term_a",
        anchor=None,
        request_id=None,
        created_at="2026-01-02T00:00:00Z",
    )
    first = read(store, "read_runs", caller="term_fork", limit=1)
    assert first["thread"] == "term_fork"
    assert first["head"] == "term_fork@0"
    store.rewind_thread(
        thread_id="term_fork",
        anchor="run_a",
        request_id=None,
        expected_head=ControlRef.parse(first["head"]),
        created_at="2026-01-03T00:00:00Z",
    )
    start(store, "run_new", thread="term_fork")
    project_run_end(store, run_id="run_new")
    with closing(RunStore(store.db_path, read_only=True)) as reopened:
        pages = collect(reopened, "read_runs", first)
    records = [r for p in pages for r in p["runs"]]
    assert [r["id"] for r in records] == ["run_c", "run_a", "run_b"]
    assert all(r["thread"] == "term_a" for r in records)
    assert all(p["head"] == first["head"] for p in pages)
    assert [r["id"] for r in read(store, "read_runs", thread="term_fork")["runs"]] == [
        "run_c",
        "run_new",
    ]


def test_half_open_ranges_and_tail_pages_keep_natural_order(store):
    for name in ("run_a", "run_b", "run_c", "run_d"):
        start(store, name)
        project_run_end(store, run_id=name)
    page = read(store, "read_runs", begin="run_a", end="run_d", from_end=True, limit=2)
    assert [
        [r["id"] for r in p["runs"]] for p in collect(store, "read_runs", page)
    ] == [["run_b", "run_c"], ["run_a"]]
    assert read(store, "read_runs", begin="run_b", end="run_b")["runs"] == []
    for index in (10, 2, 0):
        step(store, index=index)
    page = read(
        store,
        "read_steps",
        run="run_a",
        begin="run_a.0",
        end="run_a.10",
        from_end=True,
        limit=1,
    )
    assert [
        [r["id"] for r in p["entries"]] for p in collect(store, "read_steps", page)
    ] == [["run_a.2"], ["run_a.0"]]
    empty = read(store, "read_steps", run="run_a", begin="run_a.2", end="run_a.2")
    assert empty["entries"] == empty["dependencies"] == []
    assert empty["cursor"] is None


@pytest.mark.parametrize(
    "tool,query",
    [
        ("read_runs", {"thread": "run_a"}),
        ("read_runs", {"begin": "run_b", "end": "run_a"}),
        ("read_runs", {"begin": "run_foreign"}),
        ("read_runs", {"begin": "run_a.0"}),
        ("read_steps", {"run": "run_a", "begin": "run_b.0"}),
        ("read_steps", {"run": "run_a", "begin": "run_a.1", "end": "run_a.0"}),
        ("read_output", {"run": "run_a.0"}),
    ],
)
def test_invalid_and_foreign_bounds_fail(store, tool, query):
    for name in ("run_a", "run_b"):
        start(store, name)
        step(store, name)
        step(store, name, 1)
    with pytest.raises((ValueError, TypeError)):
        read(store, tool, **query)


@pytest.mark.parametrize(
    "tool,query",
    [
        ("read_runs", {"thread": "term_missing"}),
        ("read_steps", {"run": "run_missing"}),
        ("read_output", {"run": "run_missing"}),
    ],
)
def test_missing_targets_fail(store, tool, query):
    with pytest.raises(KeyError):
        read(store, tool, **query)


def test_cursors_reject_other_tools_and_agents(store, tmp_path):
    for name in ("run_a", "run_b"):
        start(store, name)
        project_run_end(store, run_id=name)
    cursor = read(store, "read_runs", limit=1)["cursor"]
    for tool in ("read_threads", "read_steps"):
        with pytest.raises(ValueError, match="another agent or tool"):
            read(store, tool, cursor=cursor)
    with closing(RunStore(tmp_path / "other.db")) as other:
        with pytest.raises(ValueError, match="another agent or tool"):
            read(other, "read_runs", cursor=cursor)


def test_steps_resolve_locals_and_keep_dependencies_and_model_refs(store, monkeypatch):
    start(store)
    source = step(store, output=Local("exact input"))
    steer = store.accept_run_control(
        run_id="run_a",
        kind="steer",
        timing="next_step",
        locals=(Local.typed("Part[]", (TextPart("exact input"),), "_", 0),),
        request_id=None,
        created_at="2026-01-01T00:00:03Z",
    )
    model = store.begin_step(
        ref=StepRef.parse("run_a.1"),
        kind="model",
        input=(),
        given=ModelStepGiven("test", ModelCall("instructions", [])),
        preceded_by=(steer.ref,),
        started_at="2026-01-01T00:00:04Z",
    )
    store.finish_step(
        ref=model.ref,
        kind="model",
        status="canceled",
        output=Local.typed(
            "Text", FieldRef.from_path(source.ref, "output", "value"), None, 0
        ),
        noted=None,
        error=None,
        finished_at="2026-01-01T00:00:05Z",
    )
    unused = project_run_control(
        store, run_id="run_a", kind="cancel", input=Message.user("unused")
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("history tools must not rebuild model calls")

    monkeypatch.setattr(store, "rebuild_model_calls", forbidden)
    page = read(store, "read_steps", run="run_a", begin="run_a.1")
    assert [r["id"] for r in page["entries"]] == [model.id]
    assert page["entries"][0]["given"] == record_to_data(model)["given"]
    assert page["entries"][0]["status"] == "canceled"
    assert page["entries"][0]["output"]["value"] == "exact input"
    by_id = {c["id"]: c for c in page["dependencies"]}
    assert by_id[steer.id]["payload"]["input"][0]["value"] == [
        {"type": "text", "text": "exact input"}
    ]
    assert unused.id not in by_id
    all_pages = collect(
        store, "read_steps", read(store, "read_steps", run="run_a", limit=1)
    )
    assert unused.id in [r["id"] for p in all_pages for r in p["entries"]]
    assert source.output.value == "exact input"
    assert isinstance(store.get_step(ref=model.ref).output.value, TypedRef)


def test_execute_input_is_resolved_in_entries_and_dependencies(store):
    start(store)
    arguments = {"runnable": "target", "input": {"_": {"task": "inspect"}}}
    source = step(
        store,
        kind="model",
        output=(ToolCallPart("call", "_toolang__execute", "_toolang", arguments),),
    )
    trigger = store.begin_step(
        ref=StepRef.parse("run_a.1"),
        kind="tool",
        input=(),
        given=ToolStepGiven(
            "_toolang", ToolCall("call", "provider", "_toolang__execute", arguments)
        ),
        started_at="2026-01-01T00:00:03Z",
    )
    control = store.accept_execute_control(
        run_id="run_a",
        state="0" * 64,
        runnable="agent$agic:target",
        triggered_by=trigger.ref,
        locals=(
            Local.typed(
                "Json",
                FieldRef.from_path(
                    source.ref, "output", "value", 0, "input", "input", "_"
                ),
                "_",
                0,
            ),
        ),
        created_at="2026-01-01T00:00:04Z",
    )
    store.finish_step(
        ref=trigger.ref,
        kind="tool",
        status="succeeded",
        output=Local.typed(
            "ToolResultPart",
            ToolResultPart(
                "call", "_toolang__execute", "_toolang", {"controls": [control.id]}
            ),
        ),
        noted=None,
        error=None,
        finished_at="2026-01-01T00:00:05Z",
    )
    store.begin_step(
        ref=StepRef.parse("run_a.2"),
        kind="model",
        input=(),
        given=ModelStepGiven("test", ModelCall("", [])),
        preceded_by=(control.ref,),
        started_at="2026-01-01T00:00:06Z",
    )
    whole = read(store, "read_steps", run="run_a")
    bounded = read(store, "read_steps", run="run_a", begin="run_a.2")
    for records in (whole["entries"], bounded["dependencies"]):
        serialized = next(r for r in records if r["id"] == control.id)
        assert serialized["payload"]["input"] == [
            {"type": "Json", "value": {"task": "inspect"}, "name": "_", "dim": 0}
        ]
    assert isinstance(
        store.get_run_control(run_id="run_a", index=control.index)
        .payload.input[0]
        .value,
        TypedRef,
    )


def test_tool_exchange_may_span_pages_without_losing_any_part(store):
    start(store)
    call = ToolCallPart("call", "test", "test", {})
    result = ToolResultPart("call", "test", "test", {"value": "done"})
    step(
        store,
        index=0,
        kind="model",
        output=(call, TextPart("partial")),
        status="canceled",
    )
    step(store, index=1, kind="tool", output=(result,))
    first = read(store, "read_steps", run="run_a", limit=1)
    step(store, index=2)  # Appended facts do not join the fixed page membership.
    with closing(RunStore(store.db_path, read_only=True)) as reopened:
        pages = collect(reopened, "read_steps", first)
    assert [r["id"] for p in pages for r in p["entries"]] == [
        "run_a.0",
        "run_a.1",
        "run_a@0",
    ]
    assert first["entries"][0]["output"]["value"][0]["type"] == "tool_call"
    assert pages[1]["entries"][0]["output"]["value"][0]["type"] == "tool_result"
    assert first["dependencies"] == pages[1]["dependencies"]


def test_parent_history_excludes_child_steps(store):
    start(store)
    parent = step(store)
    start(store, "run_child", parent=parent.ref)
    step(store, "run_child")
    assert [r["id"] for r in read(store, "read_runs")["runs"]] == ["run_a"]
    assert [r["id"] for r in read(store, "read_steps", run="run_a")["entries"]] == [
        "run_a.0",
        "run_a@0",
    ]
    child = read(store, "read_steps", run="run_child")
    assert [r["id"] for r in child["entries"]] == ["run_child.0", "run_child@0"]
    assert "run_a@0" in [r["id"] for r in child["dependencies"]]


@pytest.mark.parametrize(
    "tool,query", [("read_runs", {}), ("read_steps", {"run": "run_a"})]
)
def test_retry_invalidates_captured_facts(store, tool, query):
    for name in ("run_a", "run_b"):
        start(store, name)
        step(store, name)
        project_run_end(store, run_id=name)
    cursor = read(store, tool, limit=1, **query)["cursor"]
    payload = store.get_run_control(run_id="run_a", index=0).payload
    assert isinstance(payload, RunControlPayload)
    store.accept_retry(
        run_id="run_a",
        anchor=None,
        resources=payload.resources,
        limits=payload.limits,
        state=payload.state,
        sandbox="host",
        request_id=None,
        created_at="2026-02-01T00:00:00Z",
    )
    with pytest.raises(HistoryChangedError):
        read(store, tool, cursor=cursor)


@pytest.mark.parametrize("status", ["succeeded", "failed", "canceled"])
def test_output_preserves_status_and_partial_value_without_model_rebuild(
    store, monkeypatch, status
):
    start(store)
    source = step(store, output=Local("partial"))
    project_run_end(
        store,
        run_id="run_a",
        status=status,
        output=Local.typed(
            "Text", FieldRef.from_path(source.ref, "output", "value"), None, 0
        ),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("output-only must not inspect details")

    monkeypatch.setattr(RunHistory, "get_run", forbidden)
    monkeypatch.setattr(store, "rebuild_model_calls", forbidden)
    assert read(store, "read_output", run="run_a") == {
        "run": "run_a",
        "status": status,
        "output": {"type": "Text", "value": "partial", "name": None, "dim": 0},
    }


def test_absent_output_is_null_and_unresolved_output_fails(store):
    start(store)
    assert read(store, "read_output", run="run_a")["output"] is None
    project_run_end(
        store,
        run_id="run_a",
        output=Local.typed(
            "Text",
            FieldRef.from_path(StepRef.parse("run_a.99"), "output", "value"),
            None,
            0,
        ),
    )
    with pytest.raises((KeyError, ValueError)):
        read(store, "read_output", run="run_a")


@pytest.mark.parametrize("missing", [False, True])
def test_real_history_calls_are_ordinary_steps_and_replay_without_reading_again(
    tmp_path,
    missing,
):
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic task() -> Text:\n  context: none\n  user: Inspect history.\n",
        tools=load_tools(queries=("history/*",)),
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "read",
                        "provider",
                        "history__read_output" if missing else "history__read_runs",
                        {"run": "run_missing"} if missing else {},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            run = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="task"), tracer=tracer
            )
            assert run.status == "succeeded", run.error
            steps = harness.store.list_steps(run_id=run.id)
            assert steps[1].output is not None
            result = steps[1].output.value
            assert isinstance(result, ToolResultPart)
            if missing:
                assert result.error is not None
                assert "run_missing" in result.error
                assert steps[1].status == "failed"
            else:
                assert result.error is None
                assert result.output["thread"] == thread
                assert result.output["runs"][0]["id"] == run.id
            assert isinstance(steps[1].given, ToolStepGiven)
            assert steps[1].given.trigger == "model"
            assert [c.kind for c in harness.store.list_run_controls(run_id=run.id)] == [
                "run"
            ]
            returned = [
                p
                for m in harness.adapter.invocations[1].call.messages
                for p in m.parts
                if isinstance(p, ToolResultPart)
            ]
            assert returned == [result]
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_thread_page_decodes_only_selected_metadata(store, monkeypatch):
    import toolang.execution.store as store_module

    for identity in ("term_c", "term_b", "term_a"):
        store.create_thread(
            thread_id=identity, origin="chat", created_at="2026-01-01T00:00:00Z"
        )
    decoded = []
    original = store_module._thread_from_row

    def decode(row):
        decoded.append(row["id"])
        return original(row)

    monkeypatch.setattr(store_module, "_thread_from_row", decode)
    page = read(store, "read_threads", limit=1)
    assert page["threads"][0]["id"] == "term_a"
    assert decoded == ["term_a"]


def test_value_resolution_uses_the_same_snapshot_as_page_selection(store, monkeypatch):
    start(store)
    source = step(store, output=Local("original"))
    step(
        store,
        index=1,
        output=Local.typed("Text", FieldRef.from_path(source.ref, "output", "value")),
    )
    project_run_end(store, run_id="run_a")
    expected = read(store, "read_steps", run="run_a")
    payload = store.get_run_control(run_id="run_a", index=0).payload
    original = store.resolve_local
    changed = []
    with closing(RunStore(store.db_path)) as writer:

        def resolve(local):
            if not changed:
                writer.accept_retry(
                    run_id="run_a",
                    anchor=None,
                    resources=payload.resources,
                    limits=payload.limits,
                    state=payload.state,
                    sandbox="host",
                    request_id=None,
                    created_at="2026-02-01T00:00:00Z",
                )
                changed.append(True)
            return original(local)

        monkeypatch.setattr(store, "resolve_local", resolve)
        assert read(store, "read_steps", run="run_a") == expected
        assert writer.list_steps(run_id="run_a") == []


@pytest.mark.parametrize("interrupt", ["cancel", "steer"])
def test_history_read_can_be_interrupted_without_late_result_delivery(
    tmp_path, monkeypatch, interrupt
):
    release = threading.Event()
    finished = threading.Event()
    original = RunHistory.thread_page
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic task() -> Text:\n  context: none\n  user: Read history.\n",
        tools=load_tools(queries=("history/*",)),
        responses=[
            ModelCallResult(
                tool_calls=(ToolCall("read", "provider", "history__read_threads", {}),)
            ),
            ModelCallResult(message=Message.assistant("adjusted")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def blocked(reader, **query):
            loop.call_soon_threadsafe(entered.set)
            try:
                if not release.wait(timeout=5):
                    raise TimeoutError("history test reader was not released")
                return original(reader, **query)
            finally:
                finished.set()

        monkeypatch.setattr(RunHistory, "thread_page", blocked)
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="task",
                ),
                tracer=tracer,
            )
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                if interrupt == "cancel":
                    handle.cancel(timing="immediate")
                else:
                    handle.steer(Message.user("Change course."), timing="immediate")
                run = await asyncio.wait_for(handle, timeout=2)
                assert run.status == (
                    "canceled" if interrupt == "cancel" else "succeeded"
                )
                selected = [
                    s
                    for s in harness.store.list_steps(run_id=run.id)
                    if s.kind == "tool"
                ]
                assert len(selected) == 1
                result = selected[0].output
                assert result is not None and isinstance(result.value, ToolResultPart)
                assert (
                    result.value.error is not None and "canceled" in result.value.error
                )
                assert [
                    c.kind for c in harness.store.list_run_controls(run_id=run.id)
                ] == ["run", interrupt]
                assert_run_event_integrity(tracer.events)
            finally:
                release.set()
                assert await asyncio.to_thread(finished.wait, 2)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)
