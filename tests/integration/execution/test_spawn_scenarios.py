"""Independent root launches keep identity, ownership, and admission durable."""

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
from toolang.execution.records import (
    output_from_data,
    output_to_data,
    RunControlPayload,
)
from toolang.execution.types import AwaitableHandle, ThreadPrefix, Output, ToolStepNoted


@pytest.mark.parametrize(
    "statement", ["spawn child", "let spawn child", "let job = spawn child"]
)
@pytest.mark.parametrize("ending", ["", "run fail", "exec successor"])
def test_spawn_root_outlives_source_and_executor_drains_it(
    tmp_path: Path, statement: str, ending: str
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
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
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
            assert handle.thread != thread and handle.thread.startswith("spawn_")
            assert child.id not in {
                r.id for r in harness.store.list_run_tree(root_run_id=parent.id)
            }
            spawned_thread = harness.store.get_thread(thread_id=handle.thread)
            assert spawned_thread is not None and spawned_thread.peer.thread == thread
            assert spawned_thread.peer.type == "agent"
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
            assert not harness.executor._tasks

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
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
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


def test_agic_spawn_returns_snapshot_without_completion_injection(tmp_path: Path):
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
                        "_toolang__spawn",
                        {"runnable": "flow:child", "input": {"_": "task"}},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("started")),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            parent = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:parent")
            )
            assert parent.status == "succeeded", (
                harness.store.resolve_error(parent.error) if parent.error else None
            )
            reply = last_tool_result(harness.adapter.invocations[-1].call)
            assert set(reply.output) == {"id", "thread", "status"}
            assert reply.output["status"] == "pending"
            assert "run-result" not in "\n".join(
                message_text(m.parts)
                for m in harness.adapter.invocations[-1].call.messages
            )
            child = harness.store.get_run(run_id=reply.output["id"])
            assert child is not None and child.parent is None

    asyncio.run(scenario())


def test_handle_record_is_distinct_from_json():
    handle = AwaitableHandle("run_test", "spawn_test")
    encoded = output_to_data(Output(handle, "job"))
    assert encoded == {
        "type": "_Awaitable",
        "value": {
            "kind": "run",
            "id": "run_test",
            "thread": "spawn_test",
            "result_type": None,
        },
        "binding": "job",
    }
    plain = output_from_data(
        output_to_data(
            Output(
                {"id": "run_test", "thread": "spawn_test", "status": "pending"}, "job"
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
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
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
            await asyncio.gather(*tuple(harness.executor._tasks))
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
                        "_toolang__spawn",
                        {"runnable": "flow:child", "input": {"_": "task"}},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("started")),
        ],
    )
    original_accept = spawn.accept
    original_launch = harness.executor._launch
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

    def fail_dispatch(*args, **kwargs):
        if kwargs.get("independent"):
            raise RuntimeError("dispatch failed")
        return original_launch(*args, **kwargs)

    def fail_admission(*args, **kwargs):
        if kwargs.get("launch_context"):
            raise RuntimeError("admission failed")
        return original_run(*args, **kwargs)

    if failure == "delivery":
        monkeypatch.setattr(spawn, "accept", interrupt_delivery)
        from toolang.execution.executor.stmts import spawn as spawn_stmt

        monkeypatch.setattr(spawn_stmt, "accept", interrupt_delivery)
    elif failure == "dispatch":
        monkeypatch.setattr(harness.executor, "_launch", fail_dispatch)
    elif failure == "commit":
        connection = harness.store._conn
        monkeypatch.setattr(harness.store, "_conn", CommitFault())
        monkeypatch.setattr(harness.store, "accept_run", fail_commit)
    else:
        monkeypatch.setattr(harness.store, "accept_run", fail_admission)

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
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
                if isinstance(e, StepEnd) and e.step == step.ref
            )
            assert end.output == step.output
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
                assert isinstance(step.noted, ToolStepNoted)
                assert step.noted.summary == f"Spawned {root.id} in {root.thread}"
            if failure == "dispatch":
                assert root.status == "failed"
                assert root.error is not None
                assert harness.store.resolve_error(root.error) == "dispatch failed"

    asyncio.run(scenario())


@pytest.mark.parametrize("dispatch_failure", [False, True])
def test_spawn_routes_background_events_and_keeps_selectable_provenance(
    tmp_path: Path, monkeypatch, dispatch_failure: bool
):
    from toolang.api.common import EventSubscription
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
        original_launch = harness.executor._launch

        def fail_dispatch(*args, **kwargs):
            if kwargs.get("independent"):
                raise RuntimeError("dispatch failed")
            return original_launch(*args, **kwargs)

        monkeypatch.setattr(harness.executor, "_launch", fail_dispatch)

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
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
            subscription = EventSubscription(
                harness.executor.stream.subscribe(
                    after=baseline,
                    thread_id=handle.thread,
                )
            )
            assert isinstance(await subscription.receive(timeout=1), ThreadCreated)
            if not dispatch_failure:
                begin = await subscription.receive(timeout=1)
                assert isinstance(begin, RunBegin) and begin.run == handle.id
                gate.release()
            while not isinstance(
                event := await subscription.receive(timeout=1), RunEnd
            ):
                assert event is not None
            assert event.run == handle.id
            assert event.status == ("failed" if dispatch_failure else "succeeded")
            assert all(
                not isinstance(e, RunBegin) or e.run != handle.id for e in tracer.events
            )
            subscription.close()

    asyncio.run(scenario())


def test_retry_restores_handle_without_relaunch_and_protects_origin(tmp_path: Path):
    from toolang.execution.types import StepRef
    from toolang.execution.schemas import record_to_data
    from toolang.execution.store import RunStore

    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent(_: Text) -> Text:
  let job = spawn child
  let unrelated = Other
  run: Inspect {{job.id}}
flow child(_: Text) -> Text:
  let untouched = Child
""",
        responses=[
            RuntimeError("transient"),
            ModelCallResult(message=Message.assistant("recovered")),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="flow:parent", primary=(TextPart("input"),)
                )
            )
            assert parent.status == "failed"
            origin = harness.store.list_steps(run_id=parent.id)[0]
            assert origin.output is not None and isinstance(
                origin.output.value, AwaitableHandle
            )
            handle = origin.output.value
            entry = harness.store.get_run_control(run_id=handle.id, index=0)
            assert entry is not None
            data = cast(dict[str, Any], record_to_data(entry))
            assert data["payload"]["launch_context"]["result"]["type_name"] == "Text"
            with pytest.raises(ValueError, match="spawn origin.*rerun"):
                harness.executor.retry(
                    parent.id,
                    setup=harness.setup,
                    state=harness.state,
                    anchor=origin.ref,
                )
            retried = await harness.executor.retry(
                parent.id,
                setup=harness.setup,
                state=harness.state,
                anchor=StepRef.from_local(parent.id, (2,)),
            )
            assert retried.status == "succeeded", (
                harness.store.resolve_error(retried.error) if retried.error else None
            )
            assert handle.id in str(harness.adapter.invocations[-1].call.messages)
            assert harness.store.get_step(ref=origin.ref) == origin
            assert (
                len(
                    [
                        r
                        for r in harness.store.list_runs()
                        if str(r.thread) == handle.thread
                    ]
                )
                == 1
            )
            # Nondestructive rewind preserves independent roots and handle references.
            harness.threads.rewind(thread_id=thread, run_id=parent.id)
            assert harness.store.get_step(ref=origin.ref) == origin
            assert harness.store.run_handle_view(handle)["id"] == handle.id
            reopened = RunStore(harness.store.db_path, read_only=True)
            try:
                restored = reopened.get_step(ref=origin.ref)
                assert restored is not None and restored.output == origin.output
                assert reopened.get_run_control(run_id=handle.id, index=0) == entry
                assert reopened.run_handle_view(handle)["id"] == handle.id
            finally:
                reopened.close()

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
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
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
    from toolang.execution.types import ToolStepGiven

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
                        "_toolang__spawn",
                        {"runnable": "flow:child", "input": {"_": "task"}},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("reported")),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            parent = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:parent")
            )
            assert parent.status == "succeeded"
            steps = harness.store.list_steps(run_id=parent.id)
            origin = next(
                s
                for s in steps
                if isinstance(s.given, ToolStepGiven)
                and s.given.call.name == "_toolang__spawn"
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
            await asyncio.gather(*(task for task in harness.executor._tasks))
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
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
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
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
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


def test_spawn_preserves_resource_ceiling_and_captured_settings_on_retry(
    tmp_path: Path,
):
    from tests.support.execution_harness import RecordingTool

    harness = ExecutionHarness.create(
        tmp_path,
        source="""
instruct policy:
  Keep the caller policy.
flow parent(_: Text) -> Text:
  tools = alpha/*
  instruct = policy
  spawn child
agic child(_: Text) -> Text:
  tools = *
  user: Work on {{_}}
""",
        tools={
            "alpha__use": RecordingTool("alpha__use", output={}),
            "beta__use": RecordingTool("beta__use", output={}),
        },
        responses=[
            RuntimeError("transient"),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="flow:parent", primary=(TextPart("input"),)
                )
            )
            assert parent.status == "succeeded"
            await asyncio.gather(*(task for task in harness.executor._tasks))
            root = next(r for r in harness.store.list_runs() if r.id != parent.id)
            assert root.status == "failed"
            entry = harness.store.get_run_control(run_id=root.id, index=0)
            assert entry is not None and isinstance(entry.payload, RunControlPayload)
            assert entry.payload.launch_context is not None
            assert entry.payload.launch_context.settings.instruct is not None
            assert entry.payload.launch_context.settings.instruct.name == "policy"
            retried = await harness.executor.retry(
                root.id, setup=harness.setup, state=harness.state
            )
            assert retried.status == "succeeded", (
                harness.store.resolve_error(retried.error) if retried.error else None
            )
            for invocation in harness.adapter.invocations:
                tools = {t.name for t in invocation.call.tools}
                assert "alpha__use" in tools and "beta__use" not in tools
                assert "Keep the caller policy." in invocation.call.instructions

    asyncio.run(scenario())
