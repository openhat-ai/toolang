"""Async control delivery stays within the affected execution task."""

import asyncio

import pytest

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.execution.types import AwaitableHandle, ThreadPrefix


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("action", ["cancel", "steer", "revoke"])
def test_async_controls_preserve_the_caller(tmp_path, nested, action):
    caller_gate, target_gate = AsyncGate(), AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
flow parent(_: Text) -> Text:
  let job = async run {"branch" if nested else "child"}
  run blocker
flow branch(_: Text) -> Text:
  run child
agic blocker(_: Text) -> Text:
  user: Block caller
agic child(_: Text) -> Text:
  user: Background work
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("caller")), gate=caller_gate
            ),
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("child")), gate=target_gate
            ),
            ModelCallResult(message=Message.assistant("steered")),
        ],
    )

    async def scenario():
        async with harness:
            parent = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                    primary=(TextPart("input"),),
                )
            )
            await asyncio.wait_for(
                asyncio.gather(
                    caller_gate.wait_until_entered(), target_gate.wait_until_entered()
                ),
                3,
            )
            execution = harness.executor._active[parent.run_id].execution
            assert execution is not None
            source = harness.store.list_steps(run_id=parent.run_id)[0]
            assert source.output is not None
            handle = source.output.value
            assert isinstance(handle, AwaitableHandle)
            background = execution._background_tasks[handle.id]
            target = (
                next(
                    r.id
                    for r in harness.store.list_run_tree(root_run_id=parent.run_id)
                    if r.parent is not None and r.parent.run_id == handle.id
                )
                if nested
                else handle.id
            )
            if action == "steer":
                control = harness.executor.steer(
                    run_id=target,
                    message=Message.user("New direction"),
                    timing="immediate",
                )
            else:
                control = harness.executor.cancel(run_id=target, reason="Stop target")
                if action == "revoke":
                    harness.executor.cancel_control(run_id=target, index=control.index)
            if action == "revoke":
                delivered = asyncio.Event()
                asyncio.get_running_loop().call_soon(delivered.set)
                await delivered.wait()
                assert not background.done()
                target_gate.release()
            await asyncio.wait_for(
                asyncio.gather(background, return_exceptions=True), 3
            )
            caller = harness.store.get_run(run_id=parent.run_id)
            assert caller is not None and caller.status == "running"
            target_run = harness.store.get_run(run_id=target)
            assert target_run is not None
            assert target_run.status == (
                "canceled" if action == "cancel" else "succeeded"
            )
            saved = harness.store.get_run_control(run_id=target, index=control.index)
            assert saved is not None
            assert saved.status == ("revoked" if action == "revoke" else "applied")
            if action == "cancel":
                assert target_run.error is not None
                assert "Stop target" in harness.store.resolve_error(target_run.error)
            caller_gate.release()
            assert (await parent).status == "succeeded"

    asyncio.run(scenario())


def test_wait_after_handled_interruption_observes_target_cancellation(tmp_path):
    from toolang.execution.executor.awaitables import wait

    caller_gate, target_gate = AsyncGate(), AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent():
  let job = async run child
  run blocker
agic blocker() -> Text:
  user: Block caller
agic child() -> Text:
  user: Background work
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("caller")), gate=caller_gate
            ),
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("child")), gate=target_gate
            ),
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
                    caller_gate.wait_until_entered(), target_gate.wait_until_entered()
                ),
                3,
            )
            execution = harness.executor._active[parent.run_id].execution
            assert execution is not None
            caller = execution._active_bindings[parent.run_id]
            source = harness.store.list_steps(run_id=parent.run_id)[0]
            assert source.output is not None
            handle = source.output.value
            assert isinstance(handle, AwaitableHandle)
            entered = asyncio.Event()

            async def observe():
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                try:
                    await asyncio.sleep(0)
                except asyncio.CancelledError:
                    pass  # Like a handled immediate steer, this resumes the task.
                entered.set()
                return await wait(execution, caller, handle)

            observer = asyncio.create_task(observe())
            await entered.wait()
            harness.executor.cancel(run_id=handle.id)
            result = await asyncio.wait_for(observer, 3)
            assert result.status == "canceled"
            caller_gate.release()
            assert (await parent).status == "succeeded"

    asyncio.run(scenario())
