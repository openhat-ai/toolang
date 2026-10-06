"""Async composition across completion order, ownership, and result encodings."""

import asyncio
from itertools import permutations

import pytest

from tests.support.execution_assertions import (
    assert_replayed,
    assert_run_event_integrity,
)
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    ScriptedModelTurn,
)
from toolang.base.types.message import (
    AudioPart,
    ImagePart,
    Message,
    TextPart,
    message_text,
)
from toolang.base.types.run import ModelCallResult
from toolang.cli.common.execution_progress import ProgressProjector
from toolang.execution.events import (
    StepBegin,
    StepEnd,
    run_event_from_data,
    run_event_to_data,
)
from toolang.execution.executor import RunExecutor
from toolang.execution.store import RunStore
from toolang.execution.types import (
    AwaitableHandle,
    ThreadPrefix,
    TypedRef,
    value_to_protocol_data,
)


@pytest.mark.parametrize("completed", [False, True])
def test_retry_after_restart_observes_original_target_outcome(tmp_path, completed):
    caller_gate, child_gate = AsyncGate(), AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent() -> Text:
  let job = async run child
  let run unstable
  await job
agic unstable() -> Text:
  user: Transient failure
agic child() -> Text:
  user: Work once
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("unused")),
                gate=caller_gate,
                error=RuntimeError("transient failure"),
            ),
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("original result")),
                gate=child_gate,
            ),
            ModelCallResult(message=Message.assistant("recovered")),
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
            await asyncio.wait_for(
                asyncio.gather(
                    caller_gate.wait_until_entered(), child_gate.wait_until_entered()
                ),
                3,
            )
            source = harness.store.list_steps(run_id=parent.run_id)[0]
            assert source.output is not None
            handle = source.output.value
            assert isinstance(handle, AwaitableHandle)
            if completed:
                execution = harness.executor._active[parent.run_id].execution
                assert execution is not None
                child_gate.release()
                await asyncio.wait_for(execution._background_tasks[handle.id], 3)
            caller_gate.release()
            assert (await parent).status == "failed"
            target = harness.store.get_run(run_id=handle.id)
            assert target is not None
            assert target.status == ("succeeded" if completed else "canceled")
            target_steps = harness.store.list_steps(run_id=handle.id)
            await harness.executor.stop()
            path = harness.store.db_path
            harness.store.close()
            harness.store = RunStore(path)
            harness.executor = RunExecutor(
                harness.store,
                harness.ids,
                setup=lambda: harness.setup,
                state=lambda: harness.state,
                load_state=lambda revision: harness.state,
            )
            with pytest.raises(ValueError, match="async run origin.*rerun"):
                harness.executor.retry(
                    parent.run_id,
                    setup=harness.setup,
                    state=harness.state,
                    anchor=source.ref,
                )
            assert harness.store.get_step(ref=source.ref) == source
            assert harness.store.get_run(run_id=handle.id) == target
            result = await asyncio.wait_for(
                harness.executor.retry(
                    parent.run_id, setup=harness.setup, state=harness.state
                ),
                3,
            )
            assert result.status == ("succeeded" if completed else "failed")
            if completed:
                assert (
                    harness.store.run_output_text(run_id=result.id) == "original result"
                )
            else:
                assert result.error is not None and target.error is not None
                assert harness.store.resolve_error(
                    result.error
                ) == harness.store.resolve_error(target.error)
            assert harness.store.get_step(ref=source.ref) == source
            assert harness.store.get_run(run_id=handle.id) == target
            assert harness.store.list_steps(run_id=handle.id) == target_steps
            assert len(harness.adapter.invocations) == 3
            assert not harness.adapter.pending_responses

    asyncio.run(scenario())


@pytest.mark.parametrize("order", list(permutations(("alpha", "beta", "gamma"))))
def test_nested_async_runs_complete_in_any_order(tmp_path, monkeypatch, order):
    gates = {name: AsyncGate() for name in order}
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent() -> Text:
  let branch = async run nested
  let side = async run gamma
  await branch
  await side
  await branch
flow nested() -> Text:
  let a = async run alpha
  let b = async run beta
  await a
  await b
agic alpha() -> Text:
  recall = none
  context = none
  instruct = none
  user: Case alpha.
agic beta() -> Text:
  recall = none
  context = none
  instruct = none
  user: Case beta.
agic gamma() -> Text:
  recall = none
  context = none
  instruct = none
  user: Case gamma.
""",
        responses=[],
    )
    calls = []
    tasks = {}

    async def invoke(model, request, **kwargs):
        prompt = "\n".join(message_text(message.parts) for message in request.messages)
        name = next(name for name in gates if f"Case {name}." in prompt)
        calls.append(name)
        tasks[name] = asyncio.current_task()
        await gates[name].wait()
        return ModelCallResult(message=Message.assistant(name))

    monkeypatch.setattr(harness.adapter, "invoke", invoke)
    monkeypatch.setattr(harness.adapter, "stream", invoke)

    async def scenario():
        tracer = RecordingRunTracer()
        async with harness:
            parent = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(
                asyncio.gather(*(gate.wait_until_entered() for gate in gates.values())),
                3,
            )
            steps = harness.store.list_steps(run_id=parent.run_id)
            assert [step.status for step in steps] == [
                "succeeded",
                "succeeded",
                "running",
            ]
            execution = harness.executor._active[parent.run_id].execution
            assert execution is not None
            for name in order:
                gates[name].release()
                await asyncio.wait_for(asyncio.shield(tasks[name]), 3)
            result = await asyncio.wait_for(parent, 3)
            assert result.status == "succeeded", result.error
            assert harness.store.run_output_text(run_id=result.id) == "beta"
            assert sorted(calls) == ["alpha", "beta", "gamma"]
            assert not execution._background_tasks
            assert not execution._background_owners
            assert not execution._background_horizons
            assert not harness.executor._tasks
            assert_run_event_integrity(tracer.events)
            active = {}
            projector = ProgressProjector()
            for event in tracer.events:
                # Feed the actual wire round trip, as a remote observer would.
                event = run_event_from_data(run_event_to_data(event))
                projector.handle(event)
                assert not projector._broken
                if isinstance(event, StepBegin):
                    assert event.step.run_id not in active
                    active[event.step.run_id] = event.step
                elif isinstance(event, StepEnd):
                    assert active.pop(event.step.run_id) == event.step
            assert not active
            assert projector.root_metrics.runs == 5
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("launch", ["async run", "spawn"])
@pytest.mark.parametrize("result_type", ["Report", "Report[]", "Part[]"])
def test_await_round_trips_typed_and_multimodal_results(tmp_path, launch, result_type):
    import json

    report = {"title": "Complete result ☕", "items": [{"value": 1}, {"value": None}]}
    parts = (
        TextPart("before"),
        ImagePart(file_id="image-1", filename="diagram.png"),
        AudioPart(data="ZGF0YQ==", format="wav", transcript="accepted"),
        TextPart("after"),
    )
    value = report if result_type == "Report" else [report, report]
    response = (
        Message(role="assistant", parts=parts)
        if result_type == "Part[]"
        else Message.assistant(json.dumps(value))
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
struct Report:
  title: Text
  items: Json
flow parent() -> {result_type}:
  let job = {launch} child
  let copy = await job
  await job
agic child() -> {result_type}:
  user: Return the complete result
""",
        responses=[ModelCallResult(message=response)],
    )

    async def scenario():
        tracer = RecordingRunTracer()
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                ),
                tracer=tracer,
            )
            assert root.status == "succeeded", root.error
            source, *waits = harness.store.list_steps(run_id=root.id)
            assert source.output is not None
            handle = source.output.value
            assert isinstance(handle, AwaitableHandle)
            assert handle.result_type == result_type
            for step in waits:
                assert step.output is not None
                assert isinstance(step.output.value, TypedRef)
                assert step.output.type == result_type
            assert len(harness.adapter.invocations) == 1
            for event in tracer.events:
                assert run_event_from_data(run_event_to_data(event)) == event
        store = RunStore(harness.store.db_path, read_only=True)
        try:
            saved = store.get_run(run_id=root.id)
            assert saved is not None and saved.output is not None
            if result_type == "Part[]":
                assert store.run_output(run_id=root.id) == parts
            else:
                assert (
                    value_to_protocol_data(store.resolve_value(saved.output.value))
                    == value
                )
            restored = store.get_step(ref=source.ref)
            assert restored is not None and restored.output == source.output
        finally:
            store.close()
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["branch_cancel", "root_cancel", "shutdown"])
def test_nested_async_cleanup_respects_immediate_ownership(
    tmp_path, monkeypatch, ending
):
    gates = {name: AsyncGate() for name in ("caller", "leaf", "side")}
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent() -> Text:
  let branch = async run nested
  let sibling = async run side
  run caller
flow nested() -> Text:
  let child = async run leaf
  await child
agic caller() -> Text:
  recall = none
  context = none
  instruct = none
  user: Case caller.
agic leaf() -> Text:
  recall = none
  context = none
  instruct = none
  user: Case leaf.
agic side() -> Text:
  recall = none
  context = none
  instruct = none
  user: Case side.
""",
        responses=[],
    )

    async def invoke(model, request, **kwargs):
        prompt = "\n".join(message_text(message.parts) for message in request.messages)
        name = next(name for name in gates if f"Case {name}." in prompt)
        await gates[name].wait()
        return ModelCallResult(message=Message.assistant(name))

    monkeypatch.setattr(harness.adapter, "invoke", invoke)
    monkeypatch.setattr(harness.adapter, "stream", invoke)

    async def scenario():
        tracer = RecordingRunTracer()
        async with harness:
            parent = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(
                asyncio.gather(*(gate.wait_until_entered() for gate in gates.values())),
                3,
            )
            execution = harness.executor._active[parent.run_id].execution
            assert execution is not None
            steps = harness.store.list_steps(run_id=parent.run_id)
            handles = [step.output.value for step in steps[:2] if step.output]
            assert len(handles) == 2
            branch, sibling = handles
            assert isinstance(branch, AwaitableHandle)
            assert isinstance(sibling, AwaitableHandle)
            if ending == "branch_cancel":
                branch_task = execution._background_tasks[branch.id]
                harness.executor.cancel(run_id=branch.id)
                await asyncio.wait_for(
                    asyncio.gather(branch_task, return_exceptions=True), 3
                )
                for run_id, status in (
                    (branch.id, "canceled"),
                    (sibling.id, "running"),
                    (parent.run_id, "running"),
                ):
                    run = harness.store.get_run(run_id=run_id)
                    assert run is not None and run.status == status
                gates["side"].release()
                await asyncio.wait_for(execution._background_tasks[sibling.id], 3)
                gates["caller"].release()
            elif ending == "root_cancel":
                parent.cancel()
            else:
                await asyncio.wait_for(harness.executor.stop(), 3)
            result = await asyncio.wait_for(parent, 3)
            assert result.status == (
                "succeeded" if ending == "branch_cancel" else "canceled"
            )
            tree = harness.store.list_run_tree(root_run_id=parent.run_id)
            assert len(tree) == 5
            assert all(run.status in {"succeeded", "canceled"} for run in tree)
            assert not execution._background_tasks
            assert not execution._background_owners
            assert not execution._background_horizons
            assert not harness.executor._tasks
            assert_run_event_integrity(tracer.events)
            projector = ProgressProjector()
            for event in tracer.events:
                projector.handle(event)
                assert not projector._broken
            assert projector.root_metrics.runs == 5
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())
