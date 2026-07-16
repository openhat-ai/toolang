from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from toolang.base.error import ToolangError
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import RunResult
from toolang.execution.binding import RunBinding
from toolang.execution.db import ExecutionStore, PersistSink
from toolang.execution.events import (
    RunBegin,
    RunEnd,
    RunStarting,
    RunSteering,
    RunStopping,
    StepBegin,
    StepEnd,
    TraceEvent,
)
from toolang.execution.executor import Executor, Local, _decode_agic_output
from toolang.execution.records import InputRef, OutputRef
from toolang.lang.ast import (
    AskStmt,
    DropStmt,
    GatherStmt,
    KeepStmt,
    LetStmt,
    MapStmt,
    Program,
    RankStmt,
    RepeatStmt,
    RunStmt,
    ScatterStmt,
    SeekStmt,
    Span,
    StormStmt,
    Field,
    FlowDecl,
    StructDecl,
)


FLOW_SOURCE = """
agic helper(in: Text) -> Text:
  user: {{in}}

flow pipeline(in: Text) -> Text:
  run helper
  run -> Text: inline run
  bare inline run
  seek alice helper
  seek bob -> Text: inline seek
  ask: continue?
  scatter 3 helper
  storm 4 helper par 2
  gather helper
  settle helper
  map helper par 2
  keep first 2
  keep helper par 2
  drop last 1
  rank helper top 3 par 2
  repeat 2:
    run helper
    until: done?
  let result = run helper
  let run helper
  let note: authored text
"""


def test_flow_statements_lower_to_specific_nodes() -> None:
    program = Program.from_source(FLOW_SOURCE)
    flow = program.flows[0]

    assert flow.name == "pipeline"
    assert flow.input is not None and flow.input.type_name == "Text"
    assert flow.output == "Text"
    assert [stmt.kind for stmt in flow.stmts] == [
        "run",
        "run",
        "run",
        "seek",
        "seek",
        "ask",
        "scatter",
        "storm",
        "gather",
        "settle",
        "map",
        "keep",
        "keep",
        "drop",
        "rank",
        "repeat",
        "run",
        "run",
        "let",
    ]

    assert isinstance(flow.stmts[0], RunStmt)
    assert flow.stmts[0].runnable == "helper"
    assert isinstance(flow.stmts[3], SeekStmt)
    assert flow.stmts[3].agent == "alice"
    assert isinstance(flow.stmts[5], AskStmt)
    assert flow.stmts[5].body == "continue?"
    assert isinstance(flow.stmts[6], ScatterStmt)
    assert flow.stmts[6].count == 3
    assert isinstance(flow.stmts[7], StormStmt)
    assert (flow.stmts[7].count, flow.stmts[7].par) == (4, 2)
    assert isinstance(flow.stmts[10], MapStmt)
    assert flow.stmts[10].par == 2
    assert isinstance(flow.stmts[11], KeepStmt)
    assert (flow.stmts[11].position, flow.stmts[11].count) == ("first", 2)
    assert isinstance(flow.stmts[13], DropStmt)
    assert (flow.stmts[13].position, flow.stmts[13].count) == ("last", 1)
    assert isinstance(flow.stmts[14], RankStmt)
    assert (flow.stmts[14].limit, flow.stmts[14].count, flow.stmts[14].par) == (
        "top",
        3,
        2,
    )


def test_inline_runnables_are_generated_once_and_referenced_by_name() -> None:
    program = Program.from_source(FLOW_SOURCE)
    flow = program.flows[0]
    generated = {
        agic.name: agic for agic in program.agics if agic.name.startswith("<agic:")
    }

    inline_run = flow.stmts[1]
    bare_run = flow.stmts[2]
    inline_seek = flow.stmts[4]
    repeat = flow.stmts[15]
    assert isinstance(inline_run, RunStmt)
    assert isinstance(bare_run, RunStmt)
    assert isinstance(inline_seek, SeekStmt)
    assert isinstance(repeat, RepeatStmt)
    assert inline_run.runnable in generated
    assert generated[inline_run.runnable].output == "Text"
    assert bare_run.runnable in generated
    assert generated[bare_run.runnable].messages[0].content == "bare inline run"
    assert inline_seek.runnable in generated
    assert repeat.until in generated
    assert generated[repeat.until].output == "Boolean"


def test_flow_bindings_are_independent_of_statement_kind() -> None:
    flow = Program.from_source(FLOW_SOURCE).flows[0]

    named = flow.stmts[-3]
    discarded = flow.stmts[-2]
    authored = flow.stmts[-1]
    assert isinstance(named, RunStmt) and named.binding == "result"
    assert isinstance(discarded, RunStmt) and discarded.binding is None
    assert isinstance(authored, LetStmt)
    assert (authored.binding, authored.value) == ("note", "authored text")


def test_inline_context_and_instruct_are_program_owned_declarations() -> None:
    program = Program.from_source(
        """
agic answer:
  context:
    Runtime context.
  instruct:
    Be concise.
  user: Answer now.
"""
    )
    agic = program.agics[0]

    assert agic.context == "<context:3>"
    assert agic.instruct == "<instruct:5>"
    assert program.contexts[0].name == agic.context
    assert program.contexts[0].body == "Runtime context."
    assert program.instructs[0].name == agic.instruct
    assert program.instructs[0].body == "Be concise."


def test_agic_and_flow_names_share_one_namespace() -> None:
    with pytest.raises(ToolangError, match="Duplicate runnable name"):
        Program.from_source(
            """
agic shared:
  Hello.

flow shared:
  run shared
"""
        )


def test_executor_persists_parent_and_child_run_hierarchy(tmp_path) -> None:
    child = FlowDecl(
        name="child",
        params_explicit=True,
        stmts=(LetStmt(value="done", span=Span(line=2)),),
        span=Span(line=1),
    )
    parent = FlowDecl(
        name="parent",
        params_explicit=True,
        stmts=(RunStmt(runnable="child", span=Span(line=5)),),
        span=Span(line=4),
    )
    context, binding = _executor_fixture(tmp_path, parent, child)
    store = ExecutionStore(tmp_path / "runs.db")
    sink = PersistSink(store)
    sink.on_event(_starting(binding, parent))

    result = asyncio.run(Executor(context, emit=sink.on_event).run(binding, parent))

    assert result == Local("done", "item")
    runs = store.list_runs(limit=None, include_superseded=True)
    assert len(runs) == 2
    root = next(run for run in runs if run.id == binding.run_id)
    child_run = next(run for run in runs if run.id != binding.run_id)
    assert (root.status, root.output.step if root.output else None) == (
        "finished",
        f"{binding.run_id}/0",
    )
    assert child_run.parent == f"{binding.run_id}/0"
    assert child_run.status == "finished"
    assert [
        (step.path, step.kind, step.status) for step in store.list_steps(run_id=root.id)
    ] == [(f"{binding.run_id}/0", "run", "finished")]
    assert [
        (step.index, step.kind, step.status)
        for step in store.list_steps(run_id=child_run.id)
    ] == [(0, "system", "finished")]
    store.close()


def test_executor_map_preserves_input_order(tmp_path) -> None:
    identity = FlowDecl(
        name="identity",
        params_explicit=True,
        span=Span(line=1),
    )
    flow = FlowDecl(
        name="map_values",
        params_explicit=True,
        stmts=(MapStmt(runnable="identity", par=2, span=Span(line=4)),),
        span=Span(line=3),
    )
    context, binding = _executor_fixture(tmp_path, flow, identity)
    events: list[TraceEvent] = []
    executor = Executor(context, emit=events.append)

    result = asyncio.run(
        executor.run(binding, flow, locals={"_": Local([3, 1, 2], "list")})
    )

    assert result == Local([3, 1, 2], "list")
    child_starts = [event for event in events if isinstance(event, RunStarting)]
    assert [event.context["placement"]["item"] for event in child_starts] == [0, 1, 2]
    step_end = next(event for event in events if isinstance(event, StepEnd))
    assert step_end.step == f"{binding.run_id}/0"
    assert step_end.kind == "par"


def test_executor_positional_filters_use_system_steps(tmp_path) -> None:
    flow = FlowDecl(
        name="select_values",
        params_explicit=True,
        stmts=(
            KeepStmt(position="first", count=3, span=Span(line=2)),
            DropStmt(position="last", count=1, span=Span(line=3)),
        ),
        span=Span(line=1),
    )
    context, binding = _executor_fixture(tmp_path, flow)
    events: list[TraceEvent] = []
    executor = Executor(context, emit=events.append)

    result = asyncio.run(
        executor.run(binding, flow, locals={"_": Local([1, 2, 3, 4], "list")})
    )

    assert result == Local([1, 2], "list")
    assert [event.kind for event in events if isinstance(event, StepBegin)] == [
        "system",
        "system",
    ]
    assert not any(isinstance(event, RunStarting) for event in events)


def test_parallel_failure_ends_children_before_parent_step(tmp_path) -> None:
    invalid_child = FlowDecl(
        name="invalid_child",
        params_explicit=True,
        stmts=(GatherStmt(runnable="invalid_child", span=Span(line=2)),),
        span=Span(line=1),
    )
    flow = FlowDecl(
        name="parallel_failure",
        params_explicit=True,
        stmts=(MapStmt(runnable="invalid_child", par=2, span=Span(line=4)),),
        span=Span(line=3),
    )
    context, binding = _executor_fixture(tmp_path, flow, invalid_child)
    events: list[TraceEvent] = []

    with pytest.raises(ToolangError, match="gather requires current shape list"):
        asyncio.run(
            Executor(context, emit=events.append).run(
                binding,
                flow,
                locals={"_": Local([1, 2, 3], "list")},
            )
        )

    child_runs = {
        event.run
        for event in events
        if isinstance(event, RunStarting) and event.parent == f"{binding.run_id}/0"
    }
    child_end_positions = [
        index
        for index, event in enumerate(events)
        if isinstance(event, RunEnd) and event.run in child_runs
    ]
    parent_step_end = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, StepEnd) and event.step == f"{binding.run_id}/0"
    )
    assert child_runs
    assert len(child_end_positions) == len(child_runs)
    assert max(child_end_positions) < parent_step_end


def test_executor_repeat_uses_unique_nested_step_paths(tmp_path) -> None:
    flow = FlowDecl(
        name="repeat_values",
        params_explicit=True,
        stmts=(
            RepeatStmt(
                count=2,
                stmts=(LetStmt(value="again", span=Span(line=3)),),
                span=Span(line=2),
            ),
        ),
        span=Span(line=1),
    )
    context, binding = _executor_fixture(tmp_path, flow)
    store = ExecutionStore(tmp_path / "runs.db")
    sink = PersistSink(store)
    sink.on_event(_starting(binding, flow))

    result = asyncio.run(Executor(context, emit=sink.on_event).run(binding, flow))

    assert result == Local("again", "item")
    assert [
        (step.parent, step.index) for step in store.list_steps(run_id=binding.run_id)
    ] == [
        (binding.run_id, 0),
        (f"{binding.run_id}/0", 0),
        (f"{binding.run_id}/0", 1),
    ]
    store.close()


def test_nested_first_step_inherits_parent_basis_without_cycle(tmp_path) -> None:
    flow = FlowDecl(
        name="nested_basis",
        params_explicit=True,
        stmts=(
            LetStmt(value="before", span=Span(line=2)),
            RepeatStmt(
                count=1,
                stmts=(RepeatStmt(count=0, stmts=(), span=Span(line=4)),),
                span=Span(line=3),
            ),
        ),
        span=Span(line=1),
    )
    context, binding = _executor_fixture(tmp_path, flow)
    store = ExecutionStore(tmp_path / "runs.db")
    sink = PersistSink(store)
    sink.on_event(_starting(binding, flow))

    asyncio.run(Executor(context, emit=sink.on_event).run(binding, flow))

    steps = {step.path: step for step in store.list_steps(run_id=binding.run_id)}
    assert steps[f"{binding.run_id}/1"].input == (
        OutputRef(step=f"{binding.run_id}/0"),
    )
    assert steps[f"{binding.run_id}/1/0"].input == (
        OutputRef(step=f"{binding.run_id}/0"),
    )
    store.close()


def test_run_output_tracks_primary_binding_not_last_step(tmp_path) -> None:
    flow = FlowDecl(
        name="bindings",
        params_explicit=True,
        stmts=(
            LetStmt(value="primary", span=Span(line=2)),
            LetStmt(value="named", binding="side", span=Span(line=3)),
            LetStmt(value="discarded", binding=None, span=Span(line=4)),
        ),
        span=Span(line=1),
    )
    context, binding = _executor_fixture(tmp_path, flow)
    store = ExecutionStore(tmp_path / "runs.db")
    sink = PersistSink(store)
    sink.on_event(_starting(binding, flow))

    result = asyncio.run(Executor(context, emit=sink.on_event).run(binding, flow))

    run = store.get_run(run_id=binding.run_id)
    assert result == Local("primary", "item")
    assert run is not None
    assert run.output == OutputRef(step=f"{binding.run_id}/0")
    store.close()


def test_flow_validates_its_declared_output(tmp_path) -> None:
    flow = FlowDecl(
        name="typed",
        output="Number",
        params_explicit=True,
        stmts=(LetStmt(value="not a number", span=Span(line=2)),),
        span=Span(line=1),
    )
    context, binding = _executor_fixture(tmp_path, flow)
    events: list[TraceEvent] = []

    with pytest.raises(ToolangError, match="output is not Number"):
        asyncio.run(Executor(context, emit=events.append).run(binding, flow))

    assert isinstance(events[-1], RunEnd)
    assert events[-1].status == "failed"


def test_agic_decodes_and_validates_structured_output() -> None:
    report = StructDecl(
        name="Report",
        fields=(
            Field(name="title", type_name="Text", span=Span(line=2)),
            Field(
                name="scores",
                type_name="Number[]",
                optional=True,
                span=Span(line=3),
            ),
        ),
        span=Span(line=1),
    )
    structs = {report.name: report}

    assert _decode_agic_output(
        RunResult(output_text='{"title":"result","scores":[3,2,1]}'),
        "Report",
        structs=structs,
    ) == {"title": "result", "scores": [3, 2, 1]}

    with pytest.raises(ToolangError, match=r"output.scores\[0\] is not Number"):
        _decode_agic_output(
            RunResult(output_text='{"title":"result","scores":[true]}'),
            "Report",
            structs=structs,
        )


def test_next_call_steer_waits_for_a_calling_statement(tmp_path) -> None:
    child = FlowDecl(name="child", params_explicit=True, span=Span(line=1))
    flow = FlowDecl(
        name="steering",
        params_explicit=True,
        stmts=(
            LetStmt(value="before", span=Span(line=3)),
            RunStmt(runnable="child", span=Span(line=4)),
        ),
        span=Span(line=2),
    )
    context, binding = _executor_fixture(tmp_path, flow, child)
    store = ExecutionStore(tmp_path / "runs.db")
    sink = PersistSink(store)
    sink.on_event(_starting(binding, flow))
    sink.on_event(
        RunSteering(
            run=binding.run_id,
            cmd=1,
            input=Message.user("steered"),
            apply="next_call",
            created_at="2026-01-01T00:00:01Z",
        )
    )
    executor = Executor(
        context,
        emit=sink.on_event,
        consume_commands=lambda run, kind: store.pending_commands(
            run_id=run, kind=kind
        ),
    )

    result = asyncio.run(executor.run(binding, flow))

    steps = {step.path: step for step in store.list_steps(run_id=binding.run_id)}
    command = store.get_command(run_id=binding.run_id, index=1)
    assert result == Local("steered", "item")
    assert steps[f"{binding.run_id}/0"].input == ()
    assert steps[f"{binding.run_id}/1"].input == (InputRef(cmd=1),)
    assert command is not None and command.status == "finished"
    store.close()


def test_next_call_stop_cancels_before_the_calling_statement(tmp_path) -> None:
    child = FlowDecl(name="child", params_explicit=True, span=Span(line=1))
    flow = FlowDecl(
        name="stopping",
        params_explicit=True,
        stmts=(
            LetStmt(value="before", span=Span(line=3)),
            RunStmt(runnable="child", span=Span(line=4)),
        ),
        span=Span(line=2),
    )
    context, binding = _executor_fixture(tmp_path, flow, child)
    store = ExecutionStore(tmp_path / "runs.db")
    sink = PersistSink(store)
    sink.on_event(_starting(binding, flow))
    sink.on_event(
        RunStopping(
            run=binding.run_id,
            cmd=1,
            apply="next_call",
            input=Message.user("stop before call"),
            created_at="2026-01-01T00:00:01Z",
        )
    )
    executor = Executor(
        context,
        emit=sink.on_event,
        consume_commands=lambda run, kind: store.pending_commands(
            run_id=run, kind=kind
        ),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(executor.run(binding, flow))

    run = store.get_run(run_id=binding.run_id)
    command = store.get_command(run_id=binding.run_id, index=1)
    assert run is not None and run.status == "canceled"
    assert command is not None and command.status == "finished"
    assert [step.path for step in store.list_steps(run_id=binding.run_id)] == [
        f"{binding.run_id}/0"
    ]
    store.close()


def test_run_begin_uses_execution_time_not_acceptance_time(
    tmp_path, monkeypatch
) -> None:
    flow = FlowDecl(name="timing", params_explicit=True, span=Span(line=1))
    context, binding = _executor_fixture(tmp_path, flow)
    events: list[TraceEvent] = []
    monkeypatch.setattr(
        "toolang.execution.executor._utc_now", lambda: "2026-01-01T00:01:00Z"
    )

    asyncio.run(Executor(context, emit=events.append).run(binding, flow))

    begin = next(event for event in events if isinstance(event, RunBegin))
    assert begin.started_at == "2026-01-01T00:01:00Z"


def test_persist_sink_replays_the_same_trace_idempotently(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "runs.db")
    sink = PersistSink(store)
    trace: tuple[TraceEvent, ...] = (
        RunStarting(
            run="run_abc123",
            cmd=0,
            parent=None,
            thread="term_abc123",
            input=Message.user("hello"),
            context={"origin": "chat", "root": "run_abc123"},
            created_at="2026-01-01T00:00:00Z",
        ),
        RunBegin(
            run="run_abc123",
            input=InputRef(cmd=0),
            context={"origin": "chat", "root": "run_abc123"},
            started_at="2026-01-01T00:00:01Z",
        ),
        StepBegin(
            step="run_abc123/0",
            kind="system",
            input=(InputRef(cmd=0),),
            context={"statement": "let"},
            started_at="2026-01-01T00:00:02Z",
        ),
        StepEnd(
            step="run_abc123/0",
            kind="system",
            status="finished",
            output=(TextPart(text="done"),),
            detail={"statement": "let", "shape": "item"},
            started_at="2026-01-01T00:00:02Z",
            finished_at="2026-01-01T00:00:03Z",
        ),
        RunEnd(
            run="run_abc123",
            status="finished",
            output=OutputRef(step="run_abc123/0"),
            finished_at="2026-01-01T00:00:04Z",
        ),
    )

    for _ in range(2):
        for event in trace:
            sink.on_event(event)

    run = store.get_run(run_id="run_abc123")
    command = store.get_command(run_id="run_abc123", index=0)
    assert run is not None and command is not None
    assert (run.status, run.output, run.finished_at) == (
        "finished",
        OutputRef(step="run_abc123/0"),
        "2026-01-01T00:00:04Z",
    )
    assert command.status == "finished"
    assert len(store.list_steps(run_id="run_abc123")) == 1
    store.close()


def test_persist_sink_preserves_null_run_context_values(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "runs.db")
    sink = PersistSink(store)
    context = {
        "root": "run_abc123",
        "invoke_params": {"accumulator": None},
    }

    sink.on_event(
        RunStarting(
            run="run_abc123",
            cmd=0,
            parent=None,
            thread="term_abc123",
            input=Message.user("hello"),
            context=context,
            created_at="2026-01-01T00:00:00Z",
        )
    )
    sink.on_event(
        RunBegin(
            run="run_abc123",
            input=InputRef(cmd=0),
            context=context,
            started_at="2026-01-01T00:00:01Z",
        )
    )

    run = store.get_run(run_id="run_abc123")
    assert run is not None
    assert run.context["invoke_params"] == {"accumulator": None}
    store.close()


def test_persist_sink_rejects_conflicting_start_replay(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "runs.db")
    sink = PersistSink(store)
    start = RunStarting(
        run="run_abc123",
        cmd=0,
        parent=None,
        thread="term_abc123",
        input=Message.user("hello"),
        context={"origin": "chat"},
        created_at="2026-01-01T00:00:00Z",
    )
    sink.on_event(start)

    with pytest.raises(ValueError, match="conflicting accepted run event"):
        sink.on_event(
            RunStarting(
                run=start.run,
                cmd=start.cmd,
                parent=start.parent,
                thread=start.thread,
                input=Message.user("different"),
                context=start.context,
                created_at=start.created_at,
            )
        )
    store.close()


def _executor_fixture(tmp_path, selected: FlowDecl, *runnables: FlowDecl):
    program = SimpleNamespace(thunks=(), flows=(selected, *runnables))
    live = SimpleNamespace(program=program, fingerprint="live-test")
    context = cast(Any, SimpleNamespace(root=tmp_path, name="alice"))
    binding = RunBinding(
        run_id="run_abc123",
        group="chat",
        origin="chat",
        thread_id="term_abc123",
        thunk_name=selected.name,
        input_text="",
        message=Message.user(""),
        model_selector=None,
        model_selectors=(),
        tool_selectors=None,
        cap_selectors=(),
        run_loop="basic",
        metadata={"executable_kind": "flow"},
        live=cast(Any, live),
        created_at="2026-01-01T00:00:00Z",
    )
    return context, binding


def _starting(binding: RunBinding, flow: FlowDecl) -> RunStarting:
    return RunStarting(
        run=binding.run_id,
        cmd=0,
        parent=None,
        thread=binding.thread_id,
        input=binding.message or Message.user(binding.input_text),
        context={
            "origin": binding.origin,
            "root": binding.run_id,
            "executable": {"kind": "flow", "name": flow.name},
            "call": "top",
        },
        created_at=binding.created_at,
    )
