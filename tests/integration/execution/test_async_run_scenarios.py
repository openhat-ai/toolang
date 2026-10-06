"""Deterministic async launch/wait lifecycle and progress contracts."""

import asyncio
import json

import pytest
from pathlib import Path

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.cli.common.execution_progress.projector import ProgressProjector
from toolang.execution.events import RunBegin, StepBegin, StepEnd
from toolang.execution.types import AwaitableHandle, ThreadPrefix


def test_launch_continues_and_repeated_wait_keeps_linear_steps(tmp_path: Path):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent(_: Text) -> Text:
  let job = async run child
  let other = Continued
  let observed = await job
  await job
agic child(_: Text) -> Text:
  recall = none
  context = none
  instruct = none
  user: Child {{_}}
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("result")), gate=gate
            )
        ],
    )

    async def scenario():
        async with harness:
            tracer = RecordingRunTracer()
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            task = asyncio.ensure_future(
                harness.executor.run(
                    harness.run_spec(
                        thread=thread,
                        runnable="flow:parent",
                        primary=(TextPart("input"),),
                    ),
                    tracer=tracer,
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 3)
            root = next(
                e.run
                for e in tracer.events
                if isinstance(e, RunBegin) and e.parent is None
            )
            steps = harness.store.list_steps(run_id=root)
            assert [s.status for s in steps] == ["succeeded", "succeeded", "running"]
            output = steps[0].output
            assert output is not None
            handle = output.value
            assert isinstance(handle, AwaitableHandle) and output.type == "_Awaitable"
            assert not task.done()
            gate.release()
            parent = await asyncio.wait_for(task, 3)
            assert parent.status == "succeeded", (
                harness.store.resolve_error(parent.error) if parent.error else None
            )
            assert parent.output is not None
            assert harness.store.resolve_value(parent.output.value) == "result"
            assert len(harness.adapter.invocations) == 1
            active = None
            projector = ProgressProjector()
            for event in tracer.events:
                update = projector.handle(event)
                assert not projector._broken, update
                if isinstance(event, StepBegin) and event.step.run_id == root:
                    assert active is None
                    active = event.step
                if isinstance(event, StepEnd) and event.step.run_id == root:
                    assert active == event.step
                    active = None
            assert active is None
            assert projector.root_metrics.runs == 2
            assert not harness.executor._tasks

    asyncio.run(scenario())


@pytest.mark.parametrize("launch", ["async run", "spawn"])
@pytest.mark.parametrize(
    "value", ["text", None, ["one", "two"], [["nested"]], {"key": [1, None]}]
)
def test_await_preserves_complete_results(tmp_path, launch, value):
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
flow parent() -> Json:
  let job = {launch} child
  await job
agic child() -> Json:
  user: Return result
""",
        responses=[ModelCallResult(message=Message.assistant(json.dumps(value)))],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            parent = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="parent")
            )
            assert parent.status == "succeeded", (
                harness.store.resolve_error(parent.error) if parent.error else None
            )
            assert parent.output is not None
            from toolang.execution.types import value_to_protocol_data

            assert (
                value_to_protocol_data(harness.store.resolve_value(parent.output.value))
                == value
            )
            assert len(harness.adapter.invocations) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "wait_stmt",
    ["await job", "let output = await job", "let await job", "let job = await job"],
)
def test_no_output_await_preserves_primary_binding(tmp_path, wait_stmt):
    from dataclasses import replace
    from toolang.lang import Program

    source = f"""
flow parent(_: Text) -> Text:
  let job = async run child
  {wait_stmt}
flow child():
  let note = No primary output
"""
    program = Program.from_source(source)
    # Authored flows default to Text; exercise the runtime's absent contract.
    program = replace(
        program, flows=(program.flows[0], replace(program.flows[1], output=None))
    )
    harness = ExecutionHarness.create(
        tmp_path, source=source, program=program, responses=[]
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="parent", primary=(TextPart("input"),)
                )
            )
            assert parent.status == "succeeded", (
                harness.store.resolve_error(parent.error) if parent.error else None
            )
            assert parent.output is not None
            assert harness.store.resolve_value(parent.output.value) == "input"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "ending,status",
    [("", "succeeded"), ("run fail", "failed"), ("exec successor", "succeeded")],
)
def test_parent_end_drains_owned_work_and_keeps_launch_success(
    tmp_path, ending, status
):
    caller_gate, child_gate = AsyncGate(), AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
flow parent(_: Text) -> Text:
  let job = async run child
  run blocker
  {ending}
agic blocker(_: Text) -> Text:
  user: Block caller
agic child(_: Text) -> Text:
  user: Background
flow fail():
  let unavailable = {{{{_1._}}}}
flow successor(_: Text) -> Text:
  let note = Transferred
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("caller")), gate=caller_gate
            ),
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("child")), gate=child_gate
            ),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            tracer = RecordingRunTracer()
            task = harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="parent", primary=(TextPart("input"),)
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(
                asyncio.gather(
                    caller_gate.wait_until_entered(), child_gate.wait_until_entered()
                ),
                3,
            )
            caller_gate.release()
            parent = await asyncio.wait_for(task, 3)
            assert parent.status == status
            source = harness.store.list_steps(run_id=parent.id)[0]
            assert source.status == "succeeded" and source.output is not None
            handle = source.output.value
            assert isinstance(handle, AwaitableHandle)
            child = harness.store.get_run(run_id=handle.id)
            assert child is not None and child.status == "canceled"
            projector = ProgressProjector()
            for event in tracer.events:
                update = projector.handle(event)
                assert not projector._broken, update
            assert not harness.executor._tasks

    asyncio.run(scenario())


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("launch", ["async run", "spawn"])
def test_target_outcome_does_not_rewrite_launch(tmp_path, failed, launch):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
flow parent():
  let job = {launch} child
  await job
agic child() -> Text:
  user: Work
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("child")), gate=gate
            )
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            tracer = RecordingRunTracer()
            parent = harness.executor.run(
                harness.run_spec(thread=thread, runnable="parent"), tracer=tracer
            )
            await asyncio.wait_for(gate.wait_until_entered(), 3)
            source = harness.store.list_steps(run_id=parent.run_id)[0]
            assert source.output is not None and isinstance(
                source.output.value, AwaitableHandle
            )
            handle = source.output.value
            if failed:
                gate.fail(RuntimeError("child failed"))
            else:
                execution = harness.executor._active[parent.run_id].execution
                assert execution is not None
                harness.executor.cancel(run_id=handle.id)
            result = await asyncio.wait_for(parent, 3)
            assert result.status == "failed"
            assert (
                harness.store.list_steps(run_id=parent.run_id)[0].status == "succeeded"
            )
            assert harness.store.list_steps(run_id=parent.run_id)[1].status == "failed"
            projector = ProgressProjector()
            for event in tracer.events:
                update = projector.handle(event)
                assert not projector._broken, update

    asyncio.run(scenario())


def test_agic_async_run_and_await_share_results(tmp_path, monkeypatch):
    from toolang.base.types.run import ToolCall
    from tests.support.execution_assertions import last_tool_result

    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic parent() -> Text:
  hands = flow:child
  recall = none
  user: Start work, then await its id.
flow child(_: Text) -> Text:
  let note = Preserve input
""",
        responses=[],
    )
    calls = []

    async def invoke(model, request, *, environ, **kwargs):
        calls.append(request)
        if len(calls) == 1:
            return ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "start",
                        "start",
                        "_toolang__run",
                        {
                            "runnable": "flow:child",
                            "input": {"_": "result"},
                            "async": True,
                        },
                    ),
                )
            )
        if len(calls) == 2:
            handle = last_tool_result(request).output
            assert set(handle) == {"id", "thread", "status"}
            return ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "wait", "wait", "_toolang__await", {"target": handle["id"]}
                    ),
                )
            )
        assert last_tool_result(request).output == {"type": "Text", "value": "result"}
        return ModelCallResult(message=Message.assistant("complete"))

    monkeypatch.setattr(harness.adapter, "invoke", invoke)
    monkeypatch.setattr(harness.adapter, "stream", invoke)

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            tracer = RecordingRunTracer()
            parent = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="parent"), tracer=tracer
            )
            assert parent.status == "succeeded", (
                harness.store.resolve_error(parent.error) if parent.error else None
            )
            assert len(calls) == 3
            projector = ProgressProjector()
            for event in tracer.events:
                update = projector.handle(event)
                assert not projector._broken, update

    asyncio.run(scenario())


def test_observer_cancel_does_not_cancel_spawn_and_missing_owner_fails(tmp_path):
    from toolang.execution.executor.awaitables import wait
    from toolang.common.errors import ToolangError

    caller_gate, child_gate = AsyncGate(), AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent(_: Text) -> Text:
  let job = spawn child
  run blocker
agic blocker(_: Text) -> Text:
  user: Block
agic child(_: Text) -> Text:
  user: Child
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("caller")), gate=caller_gate
            ),
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("child")), gate=child_gate
            ),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            parent = harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="parent", primary=(TextPart("input"),)
                )
            )
            await asyncio.wait_for(
                asyncio.gather(
                    caller_gate.wait_until_entered(), child_gate.wait_until_entered()
                ),
                3,
            )
            execution = harness.executor._active[parent.run_id].execution
            assert execution is not None
            binding = execution._active_bindings[parent.run_id]
            source = harness.store.list_steps(run_id=parent.run_id)[0]
            assert source.output is not None and isinstance(
                source.output.value, AwaitableHandle
            )
            handle = source.output.value
            entered = asyncio.Event()

            async def observe():
                entered.set()
                return await wait(execution, binding, handle)

            observer = asyncio.create_task(observe())
            await entered.wait()
            observer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await observer
            target = harness.store.get_run(run_id=handle.id)
            assert target is not None and target.status == "running"
            owner = harness.executor._active.pop(handle.id)
            try:
                with pytest.raises(ToolangError, match="no live execution owner"):
                    await wait(execution, binding, handle)
            finally:
                harness.executor._active[handle.id] = owner
            caller_gate.release()
            assert (await parent).status == "succeeded"
            assert not owner.task.done()
            child_gate.release()
            await asyncio.wait_for(owner.task, 3)

    asyncio.run(scenario())


def test_retry_keeps_committed_launch_and_result(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent() -> Text:
  let job = async run child
  await job
  let run unstable
  await job
agic child() -> Text:
  user: Child
agic unstable() -> Text:
  user: Retry me
""",
        responses=[
            ModelCallResult(message=Message.assistant("child")),
            RuntimeError("temporary"),
            ModelCallResult(message=Message.assistant("recovered")),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            parent = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="parent")
            )
            assert parent.status == "failed"
            source = harness.store.list_steps(run_id=parent.id)[0]
            retried = await harness.executor.retry(
                parent.id, setup=harness.setup, state=harness.state
            )
            assert retried.status == "succeeded", (
                harness.store.resolve_error(retried.error) if retried.error else None
            )
            assert retried.output is not None
            assert harness.store.resolve_value(retried.output.value) == "child"
            assert harness.store.list_steps(run_id=parent.id)[0] == source
            assert len(harness.adapter.invocations) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["admission", "dispatch", "delivery"])
def test_async_fault_boundaries_keep_admission_and_result_consistent(
    tmp_path, monkeypatch, fault
):
    from toolang.execution.executor import awaitables
    from toolang.execution.executor.executor import _Execution

    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent() -> Text:
  let job = async run child
  await job
flow child() -> Text:
  run: Child
""",
        responses=[ModelCallResult(message=Message.assistant("child"))],
    )
    original_accept = _Execution.accept_child
    if fault == "delivery":

        async def interrupt(self, *args, **kwargs):
            await original_accept(self, *args, **kwargs)
            raise asyncio.CancelledError()

        monkeypatch.setattr(_Execution, "accept_child", interrupt)
    elif fault == "dispatch":

        def fail_start(*args, **kwargs):
            raise RuntimeError("dispatch fault")

        monkeypatch.setattr(awaitables, "start", fail_start)
    else:
        original_finish = harness.store.finish_step

        def fail_finish(*args, **kwargs):
            if isinstance(
                getattr(kwargs.get("output"), "value", None), AwaitableHandle
            ):
                raise RuntimeError("admission fault")
            return original_finish(*args, **kwargs)

        monkeypatch.setattr(harness.store, "finish_step", fail_finish)

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            tracer = RecordingRunTracer()
            parent = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="parent"), tracer=tracer
            )
            assert parent.status == ("canceled" if fault == "delivery" else "failed")
            source = harness.store.list_steps(run_id=parent.id)[0]
            tree = harness.store.list_run_tree(root_run_id=parent.id)
            if fault == "admission":
                assert (
                    source.status == "failed"
                    and source.output is None
                    and len(tree) == 1
                )
            else:
                assert (
                    source.status == "succeeded"
                    and source.output is not None
                    and len(tree) == 2
                )
                assert tree[1].status in {"failed", "canceled"}
                launch_end = next(
                    e
                    for e in tracer.events
                    if isinstance(e, StepEnd) and e.step == source.ref
                )
                assert (
                    launch_end.status == source.status
                    and launch_end.output == source.output
                )
            assert not harness.executor._tasks

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "launch", ["async run child", "let async run child", "let job = async run child"]
)
def test_unawaited_failure_and_discarded_handle_preserve_caller(tmp_path, launch):
    caller_gate, child_gate = AsyncGate(), AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
flow parent(_: Text) -> Text:
  {launch}
  let run blocker
agic blocker() -> Text:
  user: Block caller
agic child(_: Text) -> Text:
  user: Captured {{{{_}}}}
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("discarded")),
                gate=caller_gate,
            ),
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("child")), gate=child_gate
            ),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            parent = harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="parent", primary=(TextPart("original"),)
                )
            )
            await asyncio.wait_for(
                asyncio.gather(
                    caller_gate.wait_until_entered(), child_gate.wait_until_entered()
                ),
                3,
            )
            execution = harness.executor._active[parent.run_id].execution
            assert execution is not None
            child_gate.fail(RuntimeError("background failure"))
            await asyncio.gather(*execution._background_tasks.values())
            caller_gate.release()
            outcome = await parent
            assert outcome.status == "succeeded" and outcome.output is not None
            assert harness.store.resolve_value(outcome.output.value) == "original"
            source = harness.store.list_steps(run_id=parent.run_id)[0]
            assert source.status == "succeeded" and source.output is not None
            assert source.output.binding == (
                "job" if launch.startswith("let job") else None
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("lanes", [1, 2])
def test_async_run_of_map_has_one_handle_and_separate_background_scope(tmp_path, lanes):
    from toolang.execution.types import value_to_protocol_data

    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
flow parent(_: Text[]) -> Text[]:
  let job = async run mapped
  await job
flow mapped(_: Text[]) -> Text[]:
  map in {lanes} {"lane" if lanes == 1 else "lanes"} using child
agic child(_: Text) -> Text:
  user: {{{{_}}}}
""",
        responses=[
            ModelCallResult(message=Message.assistant("one")),
            ModelCallResult(message=Message.assistant("two")),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            tracer = RecordingRunTracer()
            parent = await harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="parent", named={"_": ["a", "b"]}
                ),
                tracer=tracer,
            )
            assert parent.status == "succeeded", (
                harness.store.resolve_error(parent.error) if parent.error else None
            )
            assert parent.output is not None
            assert value_to_protocol_data(
                harness.store.resolve_value(parent.output.value)
            ) == ["one", "two"]
            projector = ProgressProjector()
            rows = []
            for event in tracer.events:
                update = projector.handle(event)
                assert not projector._broken, update
                rows.extend(
                    row.text for block in update.committed for row in block.rows
                )
            assert projector.root_metrics.runs == 4
            assert "one" in "\n".join(rows) and "two" in "\n".join(rows), rows

    asyncio.run(scenario())


def test_background_budget_exhaustion_interrupts_the_root(tmp_path):
    from toolang.base.types.policy import RunLimits
    from toolang.base.types.run import ModelUsage

    caller_gate, child_gate = AsyncGate(), AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent():
  async run child
  run blocker
agic blocker() -> Text:
  user: Block caller
agic child() -> Text:
  user: Spend tokens
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("caller")), gate=caller_gate
            ),
            ScriptedModelTurn(
                ModelCallResult(
                    message=Message.assistant("child"),
                    usage=ModelUsage(input_tokens=3, output_tokens=2),
                ),
                gate=child_gate,
            ),
        ],
    )

    async def scenario():
        async with harness:
            parent = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                    limits=RunLimits(tokens=4),
                )
            )
            await asyncio.wait_for(
                asyncio.gather(
                    caller_gate.wait_until_entered(), child_gate.wait_until_entered()
                ),
                3,
            )
            child_gate.release()
            result = await asyncio.wait_for(parent, 3)
            assert result.status == "failed" and result.error is not None
            assert "Run token limit exceeded" in harness.store.resolve_error(
                result.error
            )
            assert harness.store.list_steps(run_id=result.id)[0].status == "succeeded"
            assert all(
                r.status not in {"pending", "running"}
                for r in harness.store.list_run_tree(root_run_id=result.id)
            )

    asyncio.run(scenario())


def test_async_launch_captures_each_repeat_iteration(tmp_path, monkeypatch):
    from toolang.base.types.message import message_text
    from tests.support.execution_assertions import assert_replayed

    gate = AsyncGate()
    entered = asyncio.Event()
    requests = []
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent(_: Text) -> Text:
  let label = first
  repeat 3 times:
    let job = async run child
    let label = {{label}}-next
  let run blocker
agic blocker() -> Text:
  user: Keep owner alive
agic child(_: Text, label: Text) -> Text:
  user: Current {{label}}; previous {{#_1}}{{label}}{{/_1}}{{^_1}}none{{/_1}}
""",
        responses=[],
    )

    async def invoke(model, request, *, environ, **kwargs):
        requests.append(request)
        if len(requests) == 4:
            entered.set()
        await gate.wait()
        return ModelCallResult(message=Message.assistant("done"))

    monkeypatch.setattr(harness.adapter, "invoke", invoke)
    monkeypatch.setattr(harness.adapter, "stream", invoke)

    async def scenario():
        tracer = RecordingRunTracer()
        async with harness:
            parent = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                    primary=(TextPart("first"),),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(entered.wait(), 3)
            observed = [
                message_text(message.parts)
                for request in requests
                for message in request.messages
                if message.role == "user" and "Current " in message_text(message.parts)
            ]
            assert len(observed) == 3
            for expected in [
                "Current first; previous none",
                "Current first-next; previous first-next",
                "Current first-next-next; previous first-next-next",
            ]:
                assert any(expected in message for message in observed), observed
            gate.release()
            assert (await parent).status == "succeeded"
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("launch", ["async run", "spawn"])
def test_await_rejects_forged_contracts_and_inaccessible_targets(tmp_path, launch):
    from dataclasses import replace
    from toolang.common.errors import ToolangError
    from toolang.execution.executor.awaitables import resolve, wait

    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
flow parent():
  let job = {launch} child
  await job
agic child() -> Text:
  user: Block
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("child")), gate=gate
            )
        ],
    )

    async def scenario():
        async with harness:
            parent = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 3)
            execution = harness.executor._active[parent.run_id].execution
            assert execution is not None
            caller = execution._active_bindings[parent.run_id]
            source = harness.store.list_steps(run_id=parent.run_id)[0]
            assert source.output is not None
            handle = source.output.value
            assert isinstance(handle, AwaitableHandle)
            with pytest.raises(ToolangError, match="contract mismatch"):
                await wait(execution, caller, replace(handle, result_type="Json"))
            for target in (parent.run_id, "run_missing"):
                with pytest.raises(ToolangError, match="unavailable or inaccessible"):
                    resolve(execution, caller, target)
            target_execution = harness.executor._active[handle.id].execution
            assert target_execution is not None
            target_binding = target_execution._active_bindings[handle.id]
            with pytest.raises(ToolangError, match="unavailable or inaccessible"):
                resolve(execution, target_binding, parent.run_id)
            gate.release()
            assert (await parent).status == "succeeded"

    asyncio.run(scenario())
