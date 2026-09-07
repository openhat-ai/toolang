"""Runtime-triggered Steps are facts, not fabricated model exchanges."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.support.execution_assertions import (
    assert_replayed,
    assert_run_event_integrity,
)
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    RecordingTool,
)
from toolang.base.types.message import Message, ToolResultPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.executor.steps import tool as tool_step
from toolang.execution.history import RunHistory
from toolang.execution.store import RunStore
from toolang.execution.types import ThreadPrefix, ToolStepGiven


@pytest.mark.parametrize("interrupt", [False, True])
def test_runtime_trigger_records_without_model_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: bool
) -> None:
    tool = RecordingTool("test__work", output={"done": True})
    gate = AsyncGate()
    runtime_tool = RecordingTool(
        "test__runtime", output={"done": True}, gate=gate if interrupt else None
    )
    execute = tool_step.execute

    async def with_runtime_call(state, call, **kwargs):
        result = await execute(state, call, **kwargs)
        await execute(
            state,
            ToolCall("runtime", "runtime", runtime_tool.name, {}),
            trigger="runtime",
        )
        return result

    monkeypatch.setattr(tool_step, "execute", with_runtime_call)
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic task() -> Text:
  context: none
  instruct: none
  user: Task.
""",
        tools={tool.name: tool, runtime_tool.name: runtime_tool},
        responses=[
            ModelCallResult(tool_calls=(ToolCall("model", "provider", tool.name, {}),)),
            *(
                []
                if interrupt
                else [ModelCallResult(message=Message.assistant("done"))]
            ),
            ModelCallResult(message=Message.assistant("next run")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(thread=thread, runnable="task"), tracer=tracer
            )
            if interrupt:
                await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
                handle.cancel(timing="immediate")
            first = await asyncio.wait_for(handle, timeout=2)
            assert first.status == ("canceled" if interrupt else "succeeded"), (
                first.error
            )
            steps = harness.store.list_steps(run_id=first.id)
            model, runtime = steps[1:3]
            assert (
                isinstance(runtime.given, ToolStepGiven)
                and runtime.given.trigger == "runtime"
            )
            assert (
                isinstance(model.given, ToolStepGiven)
                and model.given.trigger == "model"
            )
            assert runtime.input == ()
            assert runtime.output is not None
            assert isinstance(runtime.output.value, ToolResultPart)
            assert runtime.output.value.tool_call_id == "runtime"
            assert all(context.runtime is None for _, context in tool.calls)
            second = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="task"), tracer=tracer
            )
            assert second.status == "succeeded", second.error
            for invocation in harness.adapter.invocations[1:]:
                results = [
                    p
                    for m in invocation.call.messages
                    for p in m.parts
                    if isinstance(p, ToolResultPart)
                ]
                assert [p.tool_call_id for p in results] == ["model"]
            assert_run_event_integrity(tracer.events)
            return first.id

    run_id = asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)
    reopened = RunStore(harness.store.db_path, read_only=True)
    try:
        steps = RunHistory(reopened).run_view(run_id).steps()
        runtime = steps[2]
        assert isinstance(runtime.given, ToolStepGiven)
        assert runtime.given.trigger == "runtime"
        assert runtime.output is not None
        assert isinstance(runtime.output.value, ToolResultPart)
        assert runtime.output.value.tool_call_id == "runtime"
    finally:
        reopened.close()
