"""Independent root launches keep identity, ownership, and admission durable."""

from toolang.execution.event_projection import direct_output, step_summary


import asyncio
from typing import Any, cast
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
    RecordingRunTracer,
)
from tests.support.execution_assertions import last_tool_result
from toolang.base.types.message import Message, TextPart, message_text
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.events import ThreadCreated
from toolang.execution.records import (
    output_from_data,
    output_to_data,
    RunControlPayload,
)
from toolang.execution.types import (
    AwaitableHandle,
    ThreadKind,
    Output,
    ToolStepReport,
    StepRef,
)


@pytest.mark.parametrize("reason", [None, "cancel before entry"])
def test_wait_for_pre_entry_canceled_spawn_uses_its_own_root(
    tmp_path, monkeypatch, reason
):
    from toolang.execution.events import RunEnd
    from toolang.execution.types import RunReport

    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent() -> Text:
  let job = spawn child
  repeat 1 time:
    let tick = Tick
  await job
flow child() -> Text:
  pass
""",
        responses=[],
    )
    launch = harness.executor._launch
    children = []

    def cancel_spawn(*args, **kwargs):
        handle = launch(*args, **kwargs)
        if kwargs.get("independent"):
            children.append(handle.run_id)
            if reason is not None:
                handle.cancel(reason=reason, timing="next_step")
            handle.task.cancel()
        return handle

    monkeypatch.setattr(harness.executor, "_launch", cancel_spawn)

    async def scenario():
        loop = asyncio.get_running_loop()
        handler = loop.get_exception_handler()
        callback_errors = []
        loop.set_exception_handler(
            lambda _loop, context: callback_errors.append(context)
        )
        try:
            async with harness:
                parent = await harness.executor.run(
                    harness.run_spec(
                        thread=harness.threads.create(kind=ThreadKind.CHAT),
                        runnable="parent",
                    )
                )
                assert parent.status == "failed" and parent.error is not None
                assert harness.store.resolve_error(parent.error) == (
                    reason or "canceled"
                )
                child = harness.store.get_run(run_id=children[0])
                assert child is not None and child.status == "canceled"
                assert child.report == RunReport()
                assert child._begin_cursor is None and child._end_cursor is not None
                assert child.error is not None
                assert harness.store.resolve_error(child.error) == (
                    reason or "canceled"
                )
                if reason is not None:
                    control = harness.store.get_run_control(run_id=child.id, index=1)
                    assert control is not None and control.status == "applied"
                await harness.executor.stop()
                with harness.executor.stream.boundary() as (_, _, frames):
                    ends = [
                        frame
                        for frame in frames
                        if isinstance(frame.event, RunEnd)
                        and frame.event.run == child.id
                    ]
                assert len(ends) == 1
                assert ends[0].root_run_id == child.id
                assert ends[0].thread_id == str(child.thread)
                assert not callback_errors
                assert not harness.executor.has_owned_work()
                assert not harness.adapter.invocations
        finally:
            loop.set_exception_handler(handler)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "statement", ["spawn child", "let spawn child", "let job = spawn child"]
)
@pytest.mark.parametrize("ending", ["", "run fail", "exec successor"])
@pytest.mark.parametrize(
    "prefix",
    [ThreadKind.CHAT, ThreadKind.TALK, ThreadKind.CHORE, ThreadKind.EXEC],
)
def test_spawn_root_outlives_source_and_executor_drains_it(
    tmp_path: Path, statement: str, ending: str, prefix: ThreadKind
):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
flow parent(_: Text) -> Text:
  {statement}
  {ending}
flow fail(_: Text) -> Text:
  let unavailable = {{{{_1._}}}}
flow successor(_: Text) -> Text:
  let unchanged = Successor
agic child(_: Text) -> Text:
  recall = none
  context = none
  instruct = none
  user: Child {{{{_}}}}
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("result")), gate=gate
            )
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(kind=prefix)
            tracer = RecordingRunTracer()
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="flow:parent", primary=(TextPart("input"),)
                ),
                tracer=tracer,
            )
            assert parent.status == (
                "failed" if ending == "run fail" else "succeeded"
            ), harness.store.resolve_error(parent.error) if parent.error else None
            if parent.status == "succeeded":
                assert parent.output is not None
                assert harness.store.resolve_value(parent.output.value) == "input"
            step = harness.store.list_steps(run_id=parent.id)[0]
            assert step.kind == "spawn" and step.status == "succeeded"
            assert step.output is not None and isinstance(
                step.output.value, AwaitableHandle
            )
            handle = step.output.value
            assert output_from_data(output_to_data(step.output)) == step.output
            assert step.output.type == "_Awaitable"
            child = harness.store.get_run(run_id=handle.id)
            assert child is not None and child.parent is None
            assert handle.id.startswith("run_")
            assert handle.thread != thread and handle.thread.startswith("work_")
            assert child.id not in {
                r.id for r in harness.store.list_run_tree(root_run_id=parent.id)
            }
            spawned_thread = harness.store.get_thread(thread_id=handle.thread)
            assert spawned_thread is not None
            assert spawned_thread.kind == "work"
            with harness.executor.stream.boundary() as (_, _, frames):
                created = next(
                    frame.event
                    for frame in frames
                    if isinstance(frame.event, ThreadCreated)
                    and frame.event.thread == handle.thread
                )
            assert created.kind == "work"
            entry = harness.store.get_run_control(run_id=handle.id, index=0)
            assert entry is not None and entry.triggered_by == step.ref
            assert isinstance(entry.payload, RunControlPayload)
            assert entry.payload.launch_context is not None
            assert entry.payload.launch_context.result is not None
            assert entry.payload.launch_context.result.type_name == "Text"
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            assert harness.store.run_handle_view(handle)["status"] == "running"
            assert len(harness.adapter.invocations) == 1
            call = harness.adapter.invocations[0].call
            assert any("Child input" in message_text(m.parts) for m in call.messages), [
                message_text(m.parts) for m in call.messages
            ]
            assert all(getattr(e, "run", parent.id) != handle.id for e in tracer.events)
            await harness.executor.stop()
            assert harness.store.run_handle_view(handle)["status"] == "canceled"
            assert not harness.executor._root_runs

    asyncio.run(scenario())


def test_handle_fields_capture_as_data_and_do_not_wait(tmp_path: Path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent(_: Text) -> Text:
  let job = spawn child
  let run_id = {{job.id}}
  run: Run {{job.id}} in thread {{job.thread}} is {{job.status}}.
flow child(_: Text) -> Text:
  let untouched = Child
""",
        responses=[ModelCallResult(message=Message.assistant("summary"))],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(kind=ThreadKind.CHAT)
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="flow:parent", primary=(TextPart("input"),)
                )
            )
            assert parent.status == "succeeded", (
                harness.store.resolve_error(parent.error) if parent.error else None
            )
            steps = harness.store.list_steps(run_id=parent.id)
            assert steps[0].output is not None
            handle = steps[0].output.value
            assert isinstance(handle, AwaitableHandle)
            assert any(
                handle.id in message_text(m.parts)
                and handle.thread in message_text(m.parts)
                for m in harness.adapter.invocations[0].call.messages
            )
            assert len(harness.adapter.invocations) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "prefix",
    [ThreadKind.CHAT, ThreadKind.TALK, ThreadKind.CHORE, ThreadKind.EXEC],
)
def test_agic_spawn_returns_snapshot_without_completion_injection(
    tmp_path: Path, prefix: ThreadKind
):
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic parent() -> Text:
  recall = none
  hands = flow:child
  user: Start independent work.
flow child(_: Text) -> Text:
  let untouched = Child
""",
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "spawn-call",
                        "spawn-call",
                        "runtime__spawn",
                        {"runnable": "flow:child", "input": {"_": "task"}},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("started")),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(kind=prefix)
            parent = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:parent")
            )
            assert parent.status == "succeeded", (
                harness.store.resolve_error(parent.error) if parent.error else None
            )
            reply = last_tool_result(harness.adapter.invocations[-1].call)
            assert set(reply.output) == {"id", "thread", "status"}
            assert reply.output["status"] == "pending"
            assert reply.output["id"].startswith("run_")
            assert reply.output["thread"].startswith("work_")
            assert "run-result" not in "\n".join(
                message_text(m.parts)
                for m in harness.adapter.invocations[-1].call.messages
            )
            child = harness.store.get_run(run_id=reply.output["id"])
            assert child is not None and child.parent is None

    asyncio.run(scenario())


def test_handle_record_is_distinct_from_json():
    handle = AwaitableHandle("run_test", "work_test")
    encoded = output_to_data(Output(handle, "job"))
    assert encoded == {
        "type": "_Awaitable",
        "value": {
            "kind": "run",
            "id": "run_test",
            "thread": "work_test",
            "result_type": None,
        },
        "binding": "job",
    }
    plain = output_from_data(
        output_to_data(
            Output(
                {"id": "run_test", "thread": "work_test", "status": "pending"}, "job"
            )
        )
    )
    assert not isinstance(plain.value, AwaitableHandle)


def test_typed_spawn_keeps_result_contract_on_the_root(tmp_path: Path):
    from dataclasses import replace

    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
struct Report:
  text: Text
flow parent(_: Text) -> Text:
  let job = spawn child
agic child() -> Report[]:
  user: Work
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant('[{"text":"done"}]')),
                gate=gate,
            )
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(kind=ThreadKind.CHAT)
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="flow:parent", primary=(TextPart("input"),)
                )
            )
            assert parent.status == "succeeded", (
                harness.store.resolve_error(parent.error) if parent.error else None
            )
            step = harness.store.list_steps(run_id=parent.id)[0]
            assert step.output is not None and isinstance(
                step.output.value, AwaitableHandle
            )
            handle = step.output.value
            assert step.output.type == "_Awaitable"
            assert output_to_data(step.output)["value"] == {
                "kind": "run",
                "id": handle.id,
                "thread": handle.thread,
                "result_type": "Report[]",
            }
            entry = harness.store.get_run_control(run_id=handle.id, index=0)
            assert entry is not None and isinstance(entry.payload, RunControlPayload)
            assert entry.payload.launch_context is not None
            contract = entry.payload.launch_context.result
            assert contract is not None and contract.type_name == "Report[]"
            assert contract.definitions == {"Report": (("text", "Text", False),)}
            with pytest.raises(TypeError):
                contract.definitions["Other"] = ()  # ty: ignore[invalid-assignment]
            with pytest.raises(ValueError, match="result type is mismatched"):
                harness.store.run_handle_view(replace(handle, result_type="Text"))
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            gate.release()
            await asyncio.gather(
                *(root.task for root in tuple(harness.executor._root_runs.values()))
            )
            assert harness.store.run_handle_view(handle)["status"] == "succeeded"

    asyncio.run(scenario())


@pytest.mark.parametrize("caller", ["flow", "agic"])
@pytest.mark.parametrize("failure", ["delivery", "dispatch", "admission", "commit"])
def test_spawn_faults_preserve_atomic_admission_and_event_output(
    tmp_path: Path, monkeypatch, caller: str, failure: str
):
    from toolang.execution.executor import spawn
    from toolang.execution.events import StepEnd, run_event_from_data, run_event_to_data
    from toolang.base.types.message import ToolResultPart

    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow flow_parent(_: Text):
  let original = Original
  let job = spawn child
agic agic_parent() -> Text:
  recall = none
  hands = flow:child
  user: Start the child.
flow child(_: Text):
  let done = Done
""",
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "spawn-call",
                        "spawn-call",
                        "runtime__spawn",
                        {"runnable": "flow:child", "input": {"_": "task"}},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("started")),
        ],
    )
    original_accept = spawn.accept
    create_task = asyncio.create_task
    original_run = harness.store.accept_run
    fail_next_commit = False

    class CommitFault:
        def __getattr__(self, name):
            return getattr(connection, name)

        def commit(self):
            nonlocal fail_next_commit
            if fail_next_commit:
                fail_next_commit = False
                raise RuntimeError("commit failed")
            connection.commit()

    def fail_commit(*args, **kwargs):
        nonlocal fail_next_commit
        result = original_run(*args, **kwargs)
        if kwargs.get("launch_context"):
            fail_next_commit = True
        return result

    async def interrupt_delivery(*args, **kwargs):
        await original_accept(*args, **kwargs)
        raise asyncio.CancelledError()

    def fail_dispatch(coro, **kwargs):
        if kwargs.get("context") is not None:
            raise RuntimeError("dispatch failed")
        return create_task(coro, **kwargs)

    def fail_admission(*args, **kwargs):
        if kwargs.get("launch_context"):
            raise RuntimeError("admission failed")
        return original_run(*args, **kwargs)

    if failure == "delivery":
        monkeypatch.setattr(spawn, "accept", interrupt_delivery)
        from toolang.execution.executor.stmts import spawn as spawn_stmt

        monkeypatch.setattr(spawn_stmt, "accept", interrupt_delivery)
    elif failure == "dispatch":
        monkeypatch.setattr(asyncio, "create_task", fail_dispatch)
    elif failure == "commit":
        connection = harness.store._conn
        monkeypatch.setattr(harness.store, "_conn", CommitFault())
        monkeypatch.setattr(harness.store, "accept_run", fail_commit)
    else:
        monkeypatch.setattr(harness.store, "accept_run", fail_admission)

    async def scenario():
        async with harness:
            thread = harness.threads.create(kind=ThreadKind.CHAT)
            tracer = RecordingRunTracer()
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable=f"{caller}:{caller}_parent",
                    primary=(TextPart("input"),) if caller == "flow" else None,
                ),
                tracer=tracer,
            )
            assert parent.status == (
                "canceled"
                if failure == "delivery"
                else "succeeded"
                if failure == "dispatch"
                else "failed"
            ), harness.store.resolve_error(parent.error) if parent.error else None
            step = next(
                s
                for s in harness.store.list_steps(run_id=parent.id)
                if s.kind == ("spawn" if caller == "flow" else "tool")
            )
            end = next(
                e
                for e in tracer.events
                if isinstance(e, StepEnd) and StepRef.parse(e.step) == step.ref
            )
            assert end.output == direct_output(step.output)
            assert end.status == step.status
            assert end.finished_at == step.finished_at
            assert run_event_from_data(run_event_to_data(end)) == end
            roots = [r for r in harness.store.list_runs() if r.id != parent.id]
            if failure in {"admission", "commit"}:
                assert step.status == "failed"
                if caller == "flow":
                    assert step.output is None
                else:
                    assert step.output is not None and isinstance(
                        step.output.value, ToolResultPart
                    )
                    assert step.output.value.error and step.output.value.output == {}
                assert roots == []
                assert len(harness.store.list_threads()) == 1
                return
            assert len(roots) == 1
            root = roots[0]
            entry = harness.store.get_run_control(run_id=root.id, index=0)
            assert entry is not None and entry.status == "applied"
            assert entry.finished_at == entry.created_at == step.finished_at
            assert step.status == "succeeded"
            assert step.error is None and step.aborted_by is None
            assert root.parent is None
            assert step.output is not None
            if caller == "flow":
                assert isinstance(step.output.value, AwaitableHandle)
                assert step.output.value.id == root.id
            else:
                assert isinstance(step.output.value, ToolResultPart)
                assert step.output.value.output == {
                    "id": root.id,
                    "thread": str(root.thread),
                    "status": "pending",
                }
                assert isinstance(step.report, ToolStepReport)
                assert step.report.summary == "Spawned new run: flow:child"
                assert end.report == step_summary(step.report)
            if failure == "dispatch":
                assert root.status == "failed"
                assert root.error is not None
                assert harness.store.resolve_error(root.error) == "dispatch failed"

    asyncio.run(scenario())


@pytest.mark.parametrize("dispatch_failure", [False, True])
def test_spawn_routes_background_events_and_keeps_selectable_provenance(
    tmp_path: Path, monkeypatch, dispatch_failure: bool
):
    from toolang.execution.subscriptions import Subscriptions, StreamScope
    from toolang.execution.events import event_from_data
    from toolang.execution.events import RunBegin, RunEnd, ThreadCreated
    from toolang.execution.types import Pointer

    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent(_: Text):
  let job = spawn child
  let run_id = {{job.id}}
agic child() -> Text:
  user: Work
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("done")), gate=gate
            )
        ],
    )
    if dispatch_failure:
        create_task = asyncio.create_task

        def fail_dispatch(coro, **kwargs):
            if kwargs.get("context") is not None:
                raise RuntimeError("dispatch failed")
            return create_task(coro, **kwargs)

        monkeypatch.setattr(asyncio, "create_task", fail_dispatch)

    async def scenario():
        async with harness:
            thread = harness.threads.create(kind=ThreadKind.CHAT)
            baseline = harness.executor.stream.tail
            tracer = RecordingRunTracer()
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="flow:parent", primary=(TextPart("input"),)
                ),
                tracer=tracer,
            )
            assert parent.status == "succeeded", (
                harness.store.resolve_error(parent.error) if parent.error else None
            )
            origin, projection = harness.store.list_steps(run_id=parent.id)
            assert origin.output is not None and isinstance(
                origin.output.value, AwaitableHandle
            )
            handle = origin.output.value
            assert projection.input
            selected = harness.store.select_pointer(Pointer(projection.input[0]))
            assert selected.runtime == handle
            assert str(selected.pointer).endswith("/output/value")
            assert selected.child("id").runtime == handle.id
            assert selected.child("thread").runtime == handle.thread
            if not dispatch_failure:
                await asyncio.wait_for(gate.wait_until_entered(), 2)
            relay = Subscriptions(harness.executor.stream, harness.store.db_path)
            attachment = await relay.reserve(str(baseline))
            subscription = attachment.attach(StreamScope(thread=handle.thread))

            async def receive():
                while True:
                    frame = await subscription.receive()
                    if frame.event.startswith("stream_"):
                        continue
                    return event_from_data(frame.data)

            assert isinstance(await receive(), ThreadCreated)
            if not dispatch_failure:
                begin = await receive()
                assert isinstance(begin, RunBegin) and begin.run == handle.id
                gate.release()
            while not isinstance(event := await receive(), RunEnd):
                assert event is not None
            assert event.run == handle.id
            assert event.status == ("failed" if dispatch_failure else "succeeded")
            assert all(
                not isinstance(e, RunBegin) or e.run != handle.id for e in tracer.events
            )
            await harness.executor.stop()
            with harness.executor.stream.boundary() as (_, _, frames):
                assert (
                    sum(
                        isinstance(frame.event, RunEnd) and frame.event.run == handle.id
                        for frame in frames
                    )
                    == 1
                )
            attachment.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("caller", ["flow", "agic"])
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_spawn_does_not_hide_failed_startup_finalization(
    tmp_path, monkeypatch, caller, error_type
):
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent(_: Text):
  spawn child
agic driver(_: Text):
  recall = none
  hands = flow:child
  user: Start the child.
flow child(_: Text):
  pass
""",
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "spawn",
                        "spawn",
                        "runtime__spawn",
                        {"runnable": "flow:child", "input": {"_": "input"}},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("Started.")),
        ]
        if caller == "agic"
        else [],
    )
    create_task = asyncio.create_task
    finish_run = harness.executor._finish_run
    attempted = False

    def failed_task(coro, **kwargs):
        if kwargs.get("context") is not None:
            raise RuntimeError("task creation failed")
        return create_task(coro, **kwargs)

    def failed_terminal(*args, **kwargs):
        nonlocal attempted
        if not attempted:
            attempted = True
            raise error_type("terminal write failed")
        return finish_run(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", failed_task)
    monkeypatch.setattr(harness.executor, "_finish_run", failed_terminal)

    async def scenario():
        async with harness:
            record = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.CHAT),
                    runnable="driver" if caller == "agic" else "parent",
                    primary=(TextPart("input"),),
                )
            )
            assert record.status == "failed"
            assert record.error is not None
            assert harness.store.resolve_error(record.error) == "terminal write failed"

    asyncio.run(scenario())


def test_spawn_admission_deduplicates_and_checks_full_context(
    tmp_path: Path, monkeypatch
):
    from dataclasses import replace

    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent(_: Text) -> Text:
  spawn child
flow child(_: Text) -> Text:
  let unchanged = Child
""",
        responses=[],
    )
    original = harness.store.accept_spawn
    admissions = []

    def capture(**kwargs):
        admissions.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(harness.store, "accept_spawn", capture)

    async def scenario():
        async with harness:
            thread = harness.threads.create(kind=ThreadKind.CHAT)
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="flow:parent", primary=(TextPart("input"),)
                )
            )
            assert parent.status == "succeeded"
            arguments = admissions[0]
            handle, created = original(**arguments)
            assert not created and handle == arguments["handle"]
            assert len(harness.store.list_threads()) == 2
            for changed in (
                {"runnable": "flow:other"},
                {"cwd": "scratch://elsewhere"},
                {
                    "context": replace(
                        arguments["context"], iterations={"_1": {"_": "changed"}}
                    )
                },
            ):
                with pytest.raises(ValueError, match="conflicting spawn"):
                    original(**cast(dict[str, Any], {**arguments, **changed}))
            assert len(harness.store.list_threads()) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "hands,allowed",
    [
        ("", True),
        ("hands = *", True),
        ("hands = flow:child", True),
        ("hands = none", False),
    ],
)
def test_agic_spawn_obeys_hands_and_keeps_its_recorded_reply(
    tmp_path: Path, hands, allowed
):
    from toolang.base.types.message import ToolResultPart
    from toolang.execution.types import ToolStepSpec

    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
agic parent() -> Text:
  {hands}
  recall = none
  user: Start child and report its ID.
flow child(_: Text) -> Text:
  let unused = Work
""",
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "spawn",
                        "spawn",
                        "runtime__spawn",
                        {"runnable": "flow:child", "input": {"_": "task"}},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("reported")),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(kind=ThreadKind.CHAT)
            parent = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:parent")
            )
            assert parent.status == "succeeded"
            steps = harness.store.list_steps(run_id=parent.id)
            origin = next(
                s
                for s in steps
                if isinstance(s.spec, ToolStepSpec)
                and s.spec.call.name == "runtime__spawn"
            )
            reply = last_tool_result(harness.adapter.invocations[-1].call)
            assert origin.output is not None and isinstance(
                origin.output.value, ToolResultPart
            )
            assert origin.output.value == reply
            if not allowed:
                assert reply.error and "hands" in reply.error
                assert len(harness.store.list_runs()) == 1
                return
            assert not reply.error
            await asyncio.gather(
                *(root.task for root in tuple(harness.executor._root_runs.values()))
            )
            root = harness.store.get_run(run_id=reply.output["id"])
            assert root is not None and root.status == "succeeded"
            assert reply.output["status"] == "pending"
            rebuilt = harness.store.rebuild_model_call(steps[-1])
            assert last_tool_result(rebuilt) == reply
            assert "run-result" not in str(rebuilt.messages)

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["flow", "agic"])
def test_spawn_rejects_missing_outer_iteration_before_admission(tmp_path: Path, kind):
    body = "let prior = {{_1._}}" if kind == "flow" else "user: {{_1._}}"
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
flow parent(_: Text) -> Text:
  spawn child
{kind} child(_: Text) -> Text:
  {body}
""",
        responses=[],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(kind=ThreadKind.CHAT)
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="flow:parent", primary=(TextPart("input"),)
                )
            )
            assert parent.status == "failed" and parent.error is not None
            assert "unavailable iteration input: _1" in harness.store.resolve_error(
                parent.error
            )
            assert (
                len(harness.store.list_runs()) == len(harness.store.list_threads()) == 1
            )

    asyncio.run(scenario())


def test_repeat_spawns_distinct_roots_and_captures_handles_as_data(tmp_path: Path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent(_: Text) -> Text:
  repeat 2 times:
    let job = spawn child
    let previous = {{#_1}}{{job.id}}{{/_1}}
flow child(_: Text) -> Text:
  let untouched = Child
""",
        responses=[],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(kind=ThreadKind.CHAT)
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="flow:parent", primary=(TextPart("input"),)
                )
            )
            assert parent.status == "succeeded", (
                harness.store.resolve_error(parent.error) if parent.error else None
            )
            roots = [r for r in harness.store.list_runs() if r.id != parent.id]
            assert len(roots) == 2 and all(r.parent is None for r in roots)
            contexts = []
            for root in roots:
                entry = harness.store.get_run_control(run_id=root.id, index=0)
                assert entry is not None and isinstance(
                    entry.payload, RunControlPayload
                )
                assert entry.payload.launch_context is not None
                contexts.append(entry.payload.launch_context)
            captured = next(
                c.iterations["_1"]
                for c in contexts
                if c.iterations.get("_1") is not None
            )
            assert isinstance(captured, Mapping)
            assert set(captured["job"]) == {"id", "thread", "status"}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "caller,target_kind",
    [("flow", None), ("agic", None), *(("agic", kind) for kind in ThreadKind)],
)
@pytest.mark.parametrize("prefix", list(ThreadKind))
def test_spawn_resets_model_and_budget_while_nested_calls_inherit(
    tmp_path, caller, target_kind, prefix
):
    from dataclasses import replace
    from toolang.base.types.policy import RunDefaults, RunLimits
    from toolang.base.types.model import ModelRequest
    from tests.support.setup import replace_materialized_setup

    source = """
flow parent(_: Text) -> Text:
  run nested
  let job = spawn child
  await job
agic tool_parent(_: Text):
  {{_}}
flow nested(_: Text) -> Text:
  let note = nested
flow child(_: Text) -> Text:
  let note = child
"""
    target_thread = f"{target_kind.value}_deadbeef" if target_kind else None
    responses = (
        []
        if caller == "flow"
        else [
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "nested",
                        "nested",
                        "runtime__run",
                        {"runnable": "nested", "input": {"_": "input"}},
                    ),
                    ToolCall(
                        "spawn",
                        "spawn",
                        "runtime__spawn",
                        {
                            "runnable": "child",
                            "input": {"_": "input"},
                            **({"thread": target_thread} if target_thread else {}),
                        },
                    ),
                ),
            ),
            ModelCallResult(message=Message.assistant("complete")),
        ]
    )
    harness = ExecutionHarness.create(tmp_path, source=source, responses=responses)
    if target_thread:
        harness.store.create_thread(thread_id=target_thread)
    ordinary = harness.setup.models_effective()[0]
    router = replace(ordinary, id="router")
    normal_request = ModelRequest(ordinary.ref, max_output=512)
    harness.setup = replace_materialized_setup(
        harness.setup,
        models=(ordinary, router),
        defaults=RunDefaults(model=normal_request),
    )
    normal_limits = RunLimits(agic_model_calls=20, time=600)
    harness.setup = replace(harness.setup, limits=normal_limits)
    source_request = ModelRequest(router.ref, max_output=128)
    source_limits = RunLimits(agic_model_calls=7, time=120)

    async def scenario():
        async with harness:
            spec = harness.run_spec(
                thread=harness.threads.create(kind=prefix),
                runnable="parent" if caller == "flow" else "tool_parent",
                primary=(TextPart("input"),),
                limits=source_limits,
            )
            spec = replace(
                spec,
                bindings=replace(spec.bindings, model=router.ref),
                model_request=source_request,
            )
            parent = await harness.executor.run(spec)
            assert parent.status == "succeeded", parent.error
            await asyncio.gather(
                *(root.task for root in tuple(harness.executor._root_runs.values()))
            )
            workers = [
                run
                for run in harness.store.list_runs()
                if run.id != parent.id and run.parent is None
            ]
            assert len(workers) == 1
            if target_thread:
                assert workers[0].thread.id == target_thread
            for run in harness.store.list_runs():
                assert run.status == "succeeded", run.error
                entry = harness.store.get_run_control(run_id=run.id, index=0)
                assert entry is not None and isinstance(
                    entry.payload, RunControlPayload
                )
                control = entry.payload
                if run.id != parent.id and run.parent is None:
                    assert (
                        control.model_request == normal_request
                        and control.limits == normal_limits
                    )
                else:
                    assert (
                        control.model_request == source_request
                        and control.limits == source_limits
                    )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("source_type", "target_type", "text"),
    [
        ("Text", "Text", "input"),
        ("Part[]", "Text", "input"),
        ("Part[]", "Json", '{"answer": 42}'),
        ("Part[]", "Json", '"input"'),
        ("Part[]", "Number", "42"),
        ("Part[]", "Text[]", '["input"]'),
        ("Part[]", "Report", '{"answer": 42}'),
        ("Part[]", "Report[]", '[{"answer": 42}]'),
        ("Json", "Json", '"input"'),
    ],
)
def test_flow_spawn_replay_uses_receipt_before_changed_defaults_or_source(
    tmp_path, monkeypatch, source_type, target_type, text
):
    from dataclasses import replace
    from toolang.base.types.model import ModelRequest
    from toolang.base.types.policy import RunDefaults
    from toolang.execution.executor import spawn
    from toolang.execution.executor.common import Local
    from toolang.common.errors import ToolangError

    harness = ExecutionHarness.create(
        tmp_path,
        source=f"struct Report:\n  answer: Number\nflow parent(_: {source_type}):\n  spawn child\nflow child(_: {target_type}):\n  let note = child\n",
        responses=[],
    )
    original = spawn.accept
    seen = []

    async def replay(execution, parent, locals, step, name, **kwargs):
        handle = await original(execution, parent, locals, step, name, **kwargs)
        changed = replace(
            parent,
            setup=replace(
                parent.setup, defaults=RunDefaults(model=ModelRequest("missing/model"))
            ),
        )

        # Receipt lookup must precede target discovery, including signature changes.
        def reject(*args, **kw):
            raise AssertionError("replay resolved a new target")

        with monkeypatch.context() as context:
            context.setattr(execution, "resolve_invocation", reject)
            context.setattr(execution, "latest_state", reject)
            assert (
                await original(execution, changed, locals, step, name, **kwargs)
                == handle
            )
            with pytest.raises(ToolangError, match="conflicting spawn"):
                await original(execution, changed, locals, step, "other", **kwargs)
            with pytest.raises(ToolangError, match="conflicting spawn"):
                await original(
                    execution,
                    changed,
                    {**locals, "_": Local(value="changed", type_name="Text")},
                    step,
                    name,
                    **kwargs,
                )
        seen.append(handle)
        return handle

    monkeypatch.setattr(spawn, "accept", replay)
    monkeypatch.setattr("toolang.execution.executor.stmts.spawn.accept", replay)

    async def scenario():
        async with harness:
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.CHORE),
                    runnable="parent",
                    primary=(TextPart(text),),
                )
            )
            assert parent.status == "succeeded", parent.error
            assert len(seen) == 1 and len(harness.store.list_threads()) == 2

    asyncio.run(scenario())


def test_inline_spawn_replay_keeps_handle_snapshot_and_native_json(
    tmp_path, monkeypatch
):
    from toolang.common.errors import ToolangError
    from toolang.execution.executor import spawn
    from toolang.execution.executor.common import Local

    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent(_: Json):
  let job = spawn child
  spawn: {{job.id}} {{job.status}} {{_}}
agic child():
  Work.
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("child")), gate=gate
            ),
            ModelCallResult(message=Message.assistant("inline")),
        ],
    )
    accept = spawn.accept
    seen = []

    async def replay(execution, parent, locals, step, name, **kwargs):
        handle = await accept(execution, parent, locals, step, name, **kwargs)
        if name == "child":
            await gate.wait_until_entered()
        else:
            source = locals["job"].value
            gate.release()
            await harness.executor._root_runs[source.id].task
            # A reconstructed replay must keep the originally captured status.
            execution.reset_handle_views(step)
            assert (
                await accept(execution, parent, locals, step, name, **kwargs) == handle
            )
            with pytest.raises(ToolangError, match="conflicting spawn"):
                await accept(
                    execution,
                    parent,
                    {**locals, "job": Local(value=handle)},
                    step,
                    name,
                    **kwargs,
                )
        seen.append(handle)
        return handle

    monkeypatch.setattr("toolang.execution.executor.stmts.spawn.accept", replay)

    async def scenario():
        async with harness:
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.CHAT),
                    runnable="parent",
                    primary=(TextPart('"input"'),),
                )
            )
            assert parent.status == "succeeded", parent.error
            await asyncio.gather(
                *(root.task for root in tuple(harness.executor._root_runs.values()))
            )
            assert len(seen) == 2 and len(harness.store.list_runs()) == 3
            assert all(run.status == "succeeded" for run in harness.store.list_runs())
            inline = next(
                entry.payload
                for handle in seen
                if (entry := harness.store.get_run_control(run_id=handle.id, index=0))
                and isinstance(entry.payload, RunControlPayload)
                and "job" in entry.payload.input
            )
            captured = harness.store.resolve_value(inline.input["job"])
            assert isinstance(captured, Mapping)
            assert cast(Mapping[str, object], captured)["status"] != "succeeded"
            assert harness.store.resolve_value(inline.input["_"]) == "input"

    asyncio.run(scenario())


@pytest.mark.parametrize("preference", ["ordinary", "fallback", "excluded"])
def test_spawn_selects_ordinary_model_before_validating_target_resources(
    tmp_path, preference
):
    from dataclasses import replace
    from toolang.base.types.model import ModelRequest
    from toolang.base.types.policy import RunDefaults
    from tests.support.setup import replace_materialized_setup

    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent(_: Text):
  let job = spawn worker
  await job
agic worker(_: Text):
  models = test/scripted
  {{_}}
""",
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )
    ordinary = harness.setup.models_effective()[0]
    router = replace(ordinary, id="router")
    preferred = (
        None
        if preference == "fallback"
        else ModelRequest(
            router.ref if preference == "excluded" else ordinary.ref, max_output=128
        )
    )
    harness.setup = replace_materialized_setup(
        harness.setup, models=(router, ordinary), defaults=RunDefaults(model=preferred)
    )

    async def scenario():
        async with harness:
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.TALK),
                    runnable="parent",
                    primary=(TextPart("input"),),
                    model=router.ref,
                )
            )
            if preference == "excluded":
                assert parent.status == "failed" and len(harness.store.list_runs()) == 1
                assert not harness.adapter.invocations
                assert parent.error is not None
                assert "outside run resources" in harness.store.resolve_error(
                    parent.error
                )
            else:
                assert parent.status == "succeeded", parent.error
                assert len(harness.adapter.invocations) == 1
                assert harness.adapter.invocations[0].model.ref == ordinary.ref

    asyncio.run(scenario())


@pytest.mark.parametrize("caller", ["flow", "agic"])
def test_spawn_keeps_captured_ordinary_defaults_after_setup_refresh(
    tmp_path, monkeypatch, caller
):
    from dataclasses import replace
    from toolang.base.types.model import ModelRequest
    from toolang.base.types.policy import RunDefaults, RunLimits
    from tests.support.setup import replace_materialized_setup

    gate = AsyncGate()
    result = (
        ModelCallResult(message=Message.assistant("continue"))
        if caller == "flow"
        else ModelCallResult(
            tool_calls=(
                ToolCall(
                    "spawn",
                    "spawn",
                    "runtime__spawn",
                    {"runnable": "child", "input": {"_": "input"}},
                ),
            )
        )
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent(_: Text):
  run pause
  let job = spawn child
  await job
agic pause(_: Text):
  {{_}}
agic tool_parent(_: Text):
  {{_}}
flow child(_: Text):
  pass
""",
        responses=[
            ScriptedModelTurn(result, gate=gate),
            *(
                [ModelCallResult(message=Message.assistant("done"))]
                if caller == "agic"
                else []
            ),
        ],
    )
    ordinary = harness.setup.models_effective()[0]
    router = replace(ordinary, id="router")
    captured_request = ModelRequest(ordinary.ref, max_output=512)
    captured_limits = RunLimits(agic_model_calls=20, time=600)
    harness.setup = replace_materialized_setup(
        harness.setup,
        models=(ordinary, router),
        defaults=RunDefaults(model=captured_request),
    )
    harness.setup = replace(harness.setup, limits=captured_limits)
    monkeypatch.setattr(harness.executor, "_setup", lambda: harness.setup)
    source_request = ModelRequest(router.ref, max_output=128)
    source_limits = RunLimits(agic_model_calls=7, time=120)

    async def scenario():
        async with harness:
            spec = harness.run_spec(
                thread=harness.threads.create(kind=ThreadKind.CHORE),
                runnable="parent" if caller == "flow" else "tool_parent",
                primary=(TextPart("input"),),
                model=router.ref,
                limits=source_limits,
            )
            handle = harness.executor.run(replace(spec, model_request=source_request))
            await asyncio.wait_for(gate.wait_until_entered(), timeout=5)
            harness.setup = replace(
                harness.setup,
                defaults=RunDefaults(model=ModelRequest(router.ref, max_output=256)),
                limits=RunLimits(agic_model_calls=99, time=999),
            )
            assert harness.executor._current_setup() is harness.setup
            assert harness.setup.defaults.model != captured_request
            gate.release()
            parent = await handle
            assert parent.status == "succeeded", parent.error
            await asyncio.gather(
                *(root.task for root in tuple(harness.executor._root_runs.values()))
            )
            workers = [
                run
                for run in harness.store.list_runs()
                if run.id != parent.id and run.parent is None
            ]
            assert len(workers) == 1 and workers[0].status == "succeeded"
            for run in harness.store.list_runs():
                entry = harness.store.get_run_control(run_id=run.id, index=0)
                assert entry is not None and isinstance(
                    entry.payload, RunControlPayload
                )
                if run.id == workers[0].id:
                    assert entry.payload.model_request == captured_request
                    assert entry.payload.limits == captured_limits
                else:
                    assert entry.payload.model_request == source_request
                    assert entry.payload.limits == source_limits

    asyncio.run(scenario())


@pytest.mark.parametrize("conflict", ["handle", "thread"])
def test_spawn_replay_keeps_store_receipt_integrity_checks(
    tmp_path, monkeypatch, conflict
):
    from dataclasses import replace
    from toolang.common.errors import ToolangError
    from toolang.execution.executor import spawn

    harness = ExecutionHarness.create(
        tmp_path,
        source="flow parent(_: Text):\n  spawn child\nflow child(_: Text):\n  pass\n",
        responses=[],
    )
    accept = spawn.accept
    seen = []

    async def replay(execution, parent, locals, step, name, **kwargs):
        handle = await accept(execution, parent, locals, step, name, **kwargs)
        with monkeypatch.context() as context:
            if conflict == "handle":
                original = execution.store.get_step

                def changed_handle(*, ref):
                    record = original(ref=ref)
                    if ref == step:
                        assert record is not None
                        return replace(
                            record,
                            output=Output(
                                AwaitableHandle(handle.id, handle.thread, "Number")
                            ),
                        )
                    return record

                context.setattr(execution.store, "get_step", changed_handle)
            else:
                original_thread = execution.store.get_thread
                context.setattr(
                    execution.store,
                    "get_thread",
                    lambda *, thread_id: (
                        None
                        if thread_id == handle.thread
                        else original_thread(thread_id=thread_id)
                    ),
                )
            with pytest.raises(ToolangError, match=f"conflicting spawn {conflict}"):
                await accept(execution, parent, locals, step, name, **kwargs)
        seen.append(handle)
        return handle

    monkeypatch.setattr("toolang.execution.executor.stmts.spawn.accept", replay)

    async def scenario():
        async with harness:
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.CHAT),
                    runnable="parent",
                    primary=(TextPart("input"),),
                )
            )
            assert parent.status == "succeeded", parent.error
            assert len(seen) == 1
            await asyncio.gather(
                *(root.task for root in tuple(harness.executor._root_runs.values()))
            )

    asyncio.run(scenario())
