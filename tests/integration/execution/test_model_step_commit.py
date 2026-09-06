"""Model preparation stays disposable until its Step begin is committed."""

import asyncio
from pathlib import Path

import pytest

from tests.support.execution_assertions import (
    assert_replayed,
    assert_run_event_integrity,
    steer_message,
)
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    RecordingTool,
)
from toolang.base.types.message import Message, TextPart, ToolResultPart
from toolang.base.types.policy import RunLimits
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.events import RunEvent, StepBegin, StepEnd
from toolang.execution.executor.executor import _Execution
from toolang.execution.records import (
    ControlRecord,
    RecallControlPayload,
    StoredModelStepGiven,
)
from toolang.execution.types import (
    ControlRef,
    ControlTiming,
    FieldRef,
    SkillRecallTarget,
    ThreadPrefix,
)
from toolang.state.prepare import prepare_agent_state


@pytest.mark.parametrize("boundary", ["prepare", "before_write", "rollback"])
def test_reprepared_model_step_preserves_delta_and_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic chat(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )
    tracer = RecordingRunTracer()
    begin = _Execution.begin_step
    persist = harness.store.begin_step
    prepared = False

    def fail_write(**kwargs):
        if boundary == "rollback":
            persist(**kwargs)
        raise RuntimeError("injected begin failure")

    async def reprepare(execution, build):
        nonlocal prepared
        if not prepared:
            prepared = True
            before = tuple(execution._preceding_controls)
            assert before
            if boundary == "prepare":
                build(*execution._current_state)
            else:
                with monkeypatch.context() as patch:
                    patch.setattr(harness.store, "begin_step", fail_write)
                    with pytest.raises(RuntimeError, match="injected begin failure"):
                        await begin(execution, build)
                assert tuple(execution._preceding_controls) == before
                assert harness.store.list_steps(run_id=str(before[0].target)) == []
                assert harness.adapter.invocations == []
        return await begin(execution, build)

    monkeypatch.setattr(_Execution, "begin_step", reprepare)

    async def scenario() -> None:
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                    primary=Message.user("start").parts,
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded", run.error
            (step,) = harness.store.list_steps(run_id=run.id)
            assert step.index == 0
            assert step.preceded_by == (ControlRef.for_run(run.id, 0),)
            assert isinstance(step.given, StoredModelStepGiven)
            assert len(step.given.call.delta.messages) == 1
            assert len(harness.adapter.invocations) == 1
            assert harness.adapter.invocations[0].call.messages == [
                Message.user("start")
            ]

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("committed", [False, True])
def test_cancel_during_model_begin_records_one_canceled_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  recall = none\n  context: none\n  user: Start.\n",
        responses=[],
    )
    begin = _Execution.begin_step

    async def wait_before_begin(execution, build):
        if not committed and not gate.entered:
            await gate.wait()
        return await begin(execution, build)

    class Tracer(RecordingRunTracer):
        async def on_event(self, event: RunEvent) -> None:
            await super().on_event(event)
            if committed and isinstance(event, StepBegin):
                await gate.wait()

    tracer = Tracer()
    monkeypatch.setattr(_Execution, "begin_step", wait_before_begin)

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                    limits=RunLimits(agic_model_calls=1),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=2)
            cancel = handle.cancel(reason="stop")
            run = await asyncio.wait_for(handle, timeout=2)
            assert run.status == "canceled", run.error
            (step,) = harness.store.list_steps(run_id=run.id)
            assert step.index == 0
            assert step.preceded_by == (ControlRef.for_run(run.id, 0),)
            assert step.status == "canceled" and step.aborted_by == cancel.ref
            assert step.output is None
            assert harness.adapter.invocations == []
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("timing", ["next_call", "immediate"])
def test_controls_received_before_model_begin_enter_that_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepared: bool,
    timing: ControlTiming,
) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  recall = none\n  context: none\n  user: Start.\n",
        responses=[
            ModelCallResult(message=Message.assistant("done")),
            ModelCallResult(message=Message.assistant("unexpected extra call")),
        ],
    )
    tracer = RecordingRunTracer()
    begin = _Execution.begin_step
    expected_controls: list[ControlRecord] = []

    async def wait_before_begin(execution, build):
        if not gate.entered:
            if prepared:
                root_id = execution._active.root_run_id
                expected_controls.append(
                    harness.executor.steer(
                        run_id=root_id,
                        message=Message.user("first change"),
                        timing="next_call",
                    )
                )
                build(*execution._current_state)
            await gate.wait()
        return await begin(execution, build)

    monkeypatch.setattr(_Execution, "begin_step", wait_before_begin)

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                    limits=RunLimits(agic_model_calls=1),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=2)
            expected_controls.extend(
                (
                    handle.steer(Message.user("latest change"), timing=timing),
                    harness.store.accept_recall_control(
                        run_id=handle.run_id,
                        payload=RecallControlPayload(
                            SkillRecallTarget("testing"), "v1", "Use tests."
                        ),
                        triggered_by=None,
                        created_at="2026-09-06T00:00:00Z",
                    ),
                )
            )
            assert harness.store.list_steps(run_id=handle.run_id) == []
            gate.release()
            run = await asyncio.wait_for(handle, timeout=2)
            assert run.status == "succeeded", run.error
            (call,) = harness.adapter.invocations
            assert call.call.messages == [
                Message.user("Start."),
                *([steer_message("first change")] if prepared else []),
                steer_message("latest change"),
                Message(
                    "user",
                    (
                        TextPart('<skill ref="testing" revision="v1">'),
                        TextPart("Use tests."),
                        TextPart("</skill>"),
                    ),
                ),
            ]
            (step,) = harness.store.list_steps(run_id=run.id)
            assert step.index == 0
            assert step.preceded_by == (
                ControlRef.for_run(run.id, 0),
                *(control.ref for control in expected_controls),
            )
            for control in expected_controls:
                saved = harness.store.get_run_control(
                    run_id=run.id, index=control.index
                )
                assert saved is not None and saved.status == "applied"

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("skip_tools", [False, True])
def test_reprepared_tool_loop_preserves_messages_and_input_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skip_tools: bool
) -> None:
    tool = RecordingTool("lookup__item", output={"value": 1})
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat() -> Text:\n  recall = none\n  context: none\n  user: Start.\n",
        tools={tool.name: tool},
        responses=[
            ModelCallResult(
                tool_calls=tuple(
                    ToolCall(str(index), str(index), tool.name, {})
                    for index in range(2 if skip_tools else 1)
                )
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    steer: ControlRecord | None = None

    class Tracer(RecordingRunTracer):
        async def on_event(self, event: RunEvent) -> None:
            nonlocal steer
            await super().on_event(event)
            if skip_tools and isinstance(event, StepEnd) and event.step.index == 0:
                steer = harness.executor.steer(
                    run_id=event.step.run_id,
                    message=Message.user("skip tools"),
                    timing="next_step",
                )

    tracer = Tracer()
    begin = _Execution.begin_step

    async def reprepare(execution, build):
        build(*execution._current_state)
        return await begin(execution, build)

    monkeypatch.setattr(_Execution, "begin_step", reprepare)

    async def scenario() -> None:
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded", run.error
            steps = harness.store.list_steps(run_id=run.id)
            assert [
                len(step.given.call.delta.messages)
                for step in steps
                if isinstance(step.given, StoredModelStepGiven)
            ] == [1, 3 if skip_tools else 2]
            first, second = harness.adapter.invocations
            assert second.call.messages[:1] == first.call.messages
            assert [message.role for message in second.call.messages] == [
                "user",
                "assistant",
                "tool",
                *(["user"] if skip_tools else []),
            ]
            assert sum(
                isinstance(part, ToolResultPart)
                for message in second.call.messages
                for part in message.parts
            ) == (2 if skip_tools else 1)
            assert len(tool.calls) == (0 if skip_tools else 1)
            if skip_tools:
                assert steer is not None
                assert steps[-1].input == (
                    *(
                        FieldRef.from_path(step.ref, "output", "value")
                        for step in steps[1:3]
                    ),
                    FieldRef.from_path(steer.ref, "payload", "input", 0, "value"),
                )

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("child", [False, True])
def test_reload_and_inputs_are_adopted_in_control_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child: bool
) -> None:
    source = """
agic chat() -> Text:
  recall = none
  context: none
  instruct: original instructions
  user: Start.

flow parent() -> Text:
  run chat
"""
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )
    harness.setup.layout.program.write_text(
        source.replace("original instructions", "updated instructions"),
        encoding="utf-8",
    )
    reloaded = prepare_agent_state(harness.setup.layout)
    gate = AsyncGate()
    tracer = RecordingRunTracer()
    begin = _Execution.begin_step
    model_run_id = ""

    async def wait_before_begin(execution, build):
        nonlocal model_run_id
        candidate = build(*execution._current_state)
        if candidate.kind == "model":
            model_run_id = candidate.step.run_id
            await gate.wait()
        return await begin(execution, build)

    monkeypatch.setattr(_Execution, "begin_step", wait_before_begin)

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="flow:parent" if child else "chat",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=2)
            steer = harness.executor.steer(
                run_id=model_run_id,
                message=Message.user("change"),
                timing="next_call",
            )
            reload = handle.reload(reloaded)
            applied = await asyncio.wait_for(
                harness.executor._wait_for_control(
                    harness.executor._active[handle.run_id], reload
                ),
                timeout=2,
            )
            assert applied.status == "applied"
            recall = harness.store.accept_recall_control(
                run_id=model_run_id,
                payload=RecallControlPayload(
                    SkillRecallTarget("testing"), "v1", "Use tests."
                ),
                triggered_by=None,
                created_at="2026-09-06T00:00:00Z",
            )
            gate.release()
            run = await asyncio.wait_for(handle, timeout=2)
            assert run.status == "succeeded", run.error
            (step,) = harness.store.list_steps(run_id=model_run_id)
            entry = ControlRef.for_run(model_run_id, 0)
            expected = (
                (reload.ref, entry, steer.ref, recall.ref)
                if child
                else (entry, steer.ref, reload.ref, recall.ref)
            )
            assert step.preceded_by == expected
            assert step.state == reload.ref
            (call,) = harness.adapter.invocations
            assert "updated instructions" in call.call.instructions
            assert "original instructions" not in call.call.instructions
            assert call.call.messages[1] == steer_message("change")

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)
