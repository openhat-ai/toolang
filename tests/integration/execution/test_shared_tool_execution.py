"""Runtime-triggered Steps are facts, not fabricated model exchanges."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from toolang.base.errors import ToolangError
from toolang.base.types.tool import ToolResult
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
from toolang.common.layout import AgentLayout
from toolang.execution.executor.steps import tool as tool_step
from toolang.execution.inspection.history import RunHistory
from toolang.execution.store import RunStore
from toolang.execution.types import ThreadPrefix, ToolStepGiven
from toolang.state.prepare import prepare_agent_state
from toolang.state.watcher import StateWatcher


@pytest.mark.parametrize("phase", ["paths", "invoke"])
def test_path_errors_and_result_failures_are_recorded(tmp_path, phase):
    output = {"path": "/missing", "code": "not_found"} if phase == "invoke" else {}

    class PreparingTool(RecordingTool):
        def paths(self, arguments, context):
            if phase == "paths":
                raise ToolangError("invalid path")
            return {}

        async def invoke(self, arguments, context):
            self.calls.append((dict(arguments), context))
            return ToolResult(output, error="invalid path")

    tool = PreparingTool("test__work", output={})
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic task() -> Text:\n  context: none\n  user: Task.\n",
        tools={tool.name: tool},
        responses=[
            ModelCallResult(tool_calls=(ToolCall("call", "provider", tool.name, {}),)),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="task",
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded", run.error
            step = harness.store.list_steps(run_id=run.id)[1]
            assert step.status == "failed"
            assert step.output is not None
            assert isinstance(step.output.local.value, ToolResultPart)
            assert step.output.local.value.error == "invalid path"
            assert step.output.local.value.output == output
            results = [
                p
                for m in harness.adapter.invocations[1].call.messages
                for p in m.parts
                if isinstance(p, ToolResultPart)
            ]
            assert results == [step.output.local.value]
            assert len(tool.calls) == (0 if phase == "paths" else 1)
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


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
            assert isinstance(runtime.output.local.value, ToolResultPart)
            assert runtime.output.local.value.tool_call_id == "runtime"
            assert all(not hasattr(context, "runtime") for _, context in tool.calls)
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
        assert isinstance(runtime.output.local.value, ToolResultPart)
        assert runtime.output.local.value.tool_call_id == "runtime"
    finally:
        reopened.close()


def test_reload_keeps_runtime_routes_for_the_whole_model_batch(tmp_path: Path) -> None:
    source = """
agic parent() -> Text:
  hands = flow:permitted
  context: none
  instruct: none
  user: Parent.

flow permitted(_: Text) -> Text:
  pass

flow blocked(_: Text) -> Text:
  pass
"""
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text(source, encoding="utf-8")
    initial = prepare_agent_state(layout)
    watcher = StateWatcher(layout)
    tool = RecordingTool("test__work", output={"done": True})
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        state=initial,
        refresh_state=watcher.refresh_result,
        tools={tool.name: tool},
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall("reload", "reload", "_toolang__reload", {}),
                    ToolCall("ordinary", "ordinary", tool.name, {}),
                    ToolCall(
                        "blocked",
                        "blocked",
                        "_toolang__run",
                        {"runnable": "flow:blocked", "input": {"_": "blocked"}},
                    ),
                    ToolCall(
                        "permitted",
                        "permitted",
                        "_toolang__run",
                        {"runnable": "flow:permitted", "input": {"_": "permitted"}},
                    ),
                )
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "next",
                        "next",
                        "_toolang__run",
                        {"runnable": "flow:blocked", "input": {"_": "blocked"}},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        await watcher.refresh()
        layout.program.write_text(
            source.replace("hands = flow:permitted", "hands = flow:blocked"),
            encoding="utf-8",
        )
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                ),
                tracer=tracer,
            )
            assert root.status == "succeeded", root.error
            results = {
                step.given.call.tool_call_id: step.output.local.value
                for step in harness.store.list_steps(run_id=root.id)
                if isinstance(step.given, ToolStepGiven)
                and step.output is not None
                and isinstance(step.output.local.value, ToolResultPart)
            }
            assert (
                results["blocked"].error
                == "runnable is not authorized by hands: flow:blocked"
            )
            assert results["permitted"].error is None
            assert results["next"].error is None
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)
