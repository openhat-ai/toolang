"""Online message assembly and durable replay use the same delta semantics."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.support.execution_assertions import (
    assert_run_event_integrity,
    steer_message,
    assert_replayed,
)
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    RecordingTool,
    ScriptedModelTurn,
)
from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    message_text,
)
from toolang.base.types.run import ModelCall, ModelCallResult, ToolCall
from toolang.common.layout import AgentLayout
from toolang.execution.events import PartBegin, PartEnd, RunEvent, StepBegin, StepEnd
from toolang.execution.executor.message_buffer import _MessageBuffer
from toolang.execution.inspection.history import RunHistory
from toolang.execution.records import delta_to_data
from toolang.execution.values import parts_from_local
from toolang.execution.records import RecallControlPayload, StoredModelStepGiven
from toolang.execution.store import RunStore
from toolang.execution.types import (
    ControlTiming,
    FieldRef,
    MessageDelta,
    MessageTemplate,
    ControlRef,
    ModelStepGiven,
    RecallTarget,
    RulesRecallTarget,
    ServiceRecallTarget,
    SkillRecallTarget,
    StepRef,
    ThreadPrefix,
    TypedRef,
)
from toolang.state.prepare import prepare_agent_state


SOURCE = """
agic chat(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
"""


@pytest.mark.parametrize(
    "target",
    [
        RulesRecallTarget("project", "/src"),
        SkillRecallTarget("testing"),
        ServiceRecallTarget("github"),
    ],
)
def test_replay_after_reload_needs_only_execution_records(
    tmp_path: Path, target: RecallTarget
) -> None:
    source = """
agic chat(_: Part[]) -> Part[]:
  recall = none
  instruct: original instructions
  context: original context
  user: {{_}}
"""
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text(source, encoding="utf-8")
    state = prepare_agent_state(layout)
    gate = AsyncGate()
    tool = RecordingTool("lookup__item", output={"value": 1}, gate=gate)
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        state=state,
        tools={tool.name: tool},
        responses=[
            ModelCallResult(tool_calls=(ToolCall("lookup", "lookup", tool.name, {}),)),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                    primary=(TextPart("start"), ImagePart(file_id="image")),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=2)
            layout.program.write_text(
                source.replace("original", "updated"), encoding="utf-8"
            )
            reload = handle.reload(prepare_agent_state(layout))
            applied = await asyncio.wait_for(
                harness.executor._wait_for_control(
                    harness.executor._active[handle.run_id], reload
                ),
                timeout=2,
            )
            assert applied.status == "applied"
            harness.store.accept_recall_control(
                run_id=handle.run_id,
                payload=RecallControlPayload(target, "v1", "Use tests."),
                triggered_by=None,
                created_at="2026-09-06T00:00:00Z",
            )
            gate.release()
            run = await asyncio.wait_for(handle, timeout=2)
            assert run.status == "succeeded", run.error
            first, second = harness.adapter.invocations
            assert "original instructions" in first.call.instructions
            assert "updated instructions" in second.call.instructions
            texts = [message_text(message.parts) for message in second.call.messages]
            assert any("updated context" in text for text in texts)
            assert any("Use tests." in text for text in texts)
            assert first.call.tools and second.call.tools

    asyncio.run(scenario())
    # Remove all State layers and authored source from their runtime locations.
    # Keep them under the test directory solely for failure diagnostics.
    unavailable = tmp_path / "unavailable"
    unavailable.mkdir()
    for name, path in (
        ("root", layout.root_state),
        ("home", layout.home_state),
        ("agent", layout.agent_state),
        ("agent.too", layout.program),
    ):
        path.rename(unavailable / name)
        assert not path.exists()
    assert_replayed(harness.store.db_path, tracer.events)
    store = RunStore(harness.store.db_path, read_only=True)
    try:
        history = RunHistory(store)
        for event in tracer.events:
            if isinstance(event, StepBegin) and isinstance(event.given, ModelStepGiven):
                assert history.get_model_call(event.step) == event.given.call
    finally:
        store.close()


def test_online_tool_loops_only_record_and_render_additions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = RecordingTool("lookup__item", output={"items": [{"?": "literal"}, [1, 2]]})
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[
            *(
                ModelCallResult(tool_calls=(ToolCall(str(i), str(i), tool.name, {}),))
                for i in range(12)
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    tracer = RecordingRunTracer()
    resolved = []
    original_resolve = harness.store.resolve_value

    def resolve(value):
        resolved.append(value)
        return original_resolve(value)

    monkeypatch.setattr(harness.store, "resolve_value", resolve)
    monkeypatch.setattr(
        harness.store,
        "rebuild_model_calls",
        lambda _: pytest.fail("online execution must not rebuild history"),
    )

    async def scenario() -> None:
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                    primary=(
                        TextPart("start"),
                        ImagePart(file_id="image"),
                        AudioPart(data="YQ==", format="wav"),
                        DocumentPart(file_id="doc"),
                    ),
                ),
                tracer=tracer,
            )
            assert root.status == "succeeded", root.error
            steps = harness.store.list_steps(run_id=root.id)
            model_steps = [
                step for step in steps if isinstance(step.given, StoredModelStepGiven)
            ]
            assert [
                len(step.given.call.delta.messages)
                for step in model_steps
                if isinstance(step.given, StoredModelStepGiven)
            ] == [1, *([2] * 12)]
            for step in model_steps[1:]:
                assert isinstance(step.given, StoredModelStepGiven)
                assert all(
                    isinstance(message.segments[0], TypedRef)
                    for message in step.given.call.delta.messages
                )
            historical_outputs = {step.ref for step in steps[:-1]}
            assert not any(
                isinstance(value, TypedRef) and value.ref.record in historical_outputs
                for value in resolved
            )
            assert [
                len(item.call.messages) for item in harness.adapter.invocations
            ] == list(range(1, 27, 2))

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("kind", ["model", "plugin", "runtime"])
@pytest.mark.parametrize("interruption", ["steer", "cancel"])
def test_interruption_during_cleanup_still_persists_adopted_output(
    tmp_path: Path, kind: str, interruption: str
) -> None:
    begin_gate, end_gate = AsyncGate(), AsyncGate()
    index = 0 if kind == "model" else 1

    class Tracer(RecordingRunTracer):
        async def on_event(self, event: RunEvent) -> None:
            await super().on_event(event)
            if isinstance(event, PartBegin) and event.step.index == index:
                if not begin_gate.entered:
                    await begin_gate.wait()
            if isinstance(event, PartEnd) and event.step.index == index:
                if not end_gate.entered:
                    await end_gate.wait()

    tool = RecordingTool("lookup__item", output={"value": 1})
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[
            ModelCallResult(message=Message.assistant("draft"))
            if kind == "model"
            else ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "call",
                        "call",
                        tool.name if kind == "plugin" else "_toolang__unknown",
                        {},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("revised")),
        ],
    )
    tracer = Tracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                    primary=(TextPart("start"),),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(begin_gate.wait_until_entered(), timeout=2)
            first = handle.steer(Message.user("first change"), timing="immediate")
            await asyncio.wait_for(end_gate.wait_until_entered(), timeout=2)
            second = (
                handle.steer(Message.user("second change"), timing="immediate")
                if interruption == "steer"
                else handle.cancel()
            )
            root = await asyncio.wait_for(handle, timeout=2)
            assert root.status == (
                "succeeded" if interruption == "steer" else "canceled"
            ), root.error
            steps = harness.store.list_steps(run_id=root.id)
            step = steps[index]
            assert step.status == "canceled"
            assert step.aborted_by == second.ref
            assert step.output is not None
            assert parts_from_local(step.output.local) == tuple(
                event.data
                for event in tracer.events
                if isinstance(event, PartEnd) and event.step == step.ref
            )
            if interruption == "steer":
                assert steps[-1].preceded_by == (first.ref, second.ref)
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("interruption", ["steer", "cancel"])
@pytest.mark.parametrize("at_commit", [False, True])
@pytest.mark.parametrize("with_tool_calls", [False, True])
def test_interrupted_model_end_preserves_referenced_output(
    tmp_path: Path, interruption: str, at_commit: bool, with_tool_calls: bool
) -> None:
    gate = AsyncGate()
    blockers: list[asyncio.Task[None]] = []

    async def hold_event_lock(run_id: str) -> None:
        async with harness.executor._active[run_id].event_lock:
            await gate.wait()

    class Tracer(RecordingRunTracer):
        async def on_event(self, event: RunEvent) -> None:
            await super().on_event(event)
            if at_commit:
                if (
                    isinstance(event, PartEnd)
                    and event.step.index == 0
                    and event.part == len(requests)
                    and not blockers
                ):
                    blockers.append(
                        asyncio.create_task(hold_event_lock(event.step.run_id))
                    )
                    await asyncio.sleep(0)
            elif isinstance(event, StepEnd) and event.step.index == 0:
                await gate.wait()

    tool = RecordingTool("lookup__item", output={})
    requests = (
        tuple(ToolCall(str(i), str(i), tool.name, {}) for i in range(2))
        if with_tool_calls
        else ()
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[
            ModelCallResult(message=Message.assistant("draft"), tool_calls=requests),
            ModelCallResult(message=Message.assistant("revised")),
        ],
    )
    tracer = Tracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                    primary=(TextPart("start"),),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=2)
            control = (
                handle.steer(Message.user("change direction"), timing="immediate")
                if interruption == "steer"
                else handle.cancel()
            )
            await asyncio.sleep(0)
            gate.release()
            root = await asyncio.wait_for(handle, timeout=2)
            await asyncio.gather(*blockers)
            assert root.status == (
                "succeeded" if interruption == "steer" else "canceled"
            ), root.error
            step = harness.store.list_steps(run_id=root.id)[0]
            assert step.status == ("canceled" if at_commit else "succeeded")
            assert step.aborted_by == (control.ref if at_commit else None)
            assert step.output is not None
            parts = parts_from_local(step.output.local)
            assert parts[0] == TextPart("draft")
            assert len(parts) == 1 + len(requests)
            assert all(isinstance(part, ToolCallPart) for part in parts[1:])
            assert not tool.calls
            if interruption == "cancel":
                messages = harness.store.recent_conversation_messages(
                    thread_id=str(root.thread)
                )
                assert messages[-1] == Message.user(
                    '<toolang:cancel description="The user canceled this run."/>'
                )
            if interruption == "cancel" and requests:
                canceled_tools = [
                    item
                    for item in harness.store.list_steps(run_id=root.id)
                    if item.kind == "tool"
                ]
                assert len(canceled_tools) == len(requests)
                assert all(item.output is not None for item in canceled_tools)
            if interruption == "steer":
                results = tuple(
                    ToolResultPart(
                        tool_call_id=call.tool_call_id,
                        call_id=call.call_id,
                        tool_name=call.name,
                        tool_family=call.name,
                        error="canceled by steer",
                    )
                    for call in requests
                )
                assert harness.adapter.invocations[-1].call.messages == [
                    Message.user("start"),
                    Message("assistant", parts),
                    *([Message("tool", results)] if results else []),
                    steer_message("change direction"),
                ]
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize(
    ("timing", "interruption"),
    [("next_call", "steer"), ("immediate", "steer"), ("immediate", "cancel")],
)
@pytest.mark.parametrize("at_begin", [False, True])
def test_steer_and_interrupted_model_begin_replay_once(
    tmp_path: Path, timing: ControlTiming, interruption: str, at_begin: bool
) -> None:
    gate = AsyncGate()

    class Tracer(RecordingRunTracer):
        async def on_event(self, event: RunEvent) -> None:
            await super().on_event(event)
            if (
                at_begin
                and isinstance(event, StepBegin)
                and event.kind == "model"
                and not gate.entered
            ):
                await gate.wait()

    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("draft")),
                gate=None if at_begin else gate,
            ),
            ModelCallResult(message=Message.assistant("revised")),
        ],
    )
    tracer = Tracer()
    steer = Message(
        "user", (TextPart("new direction"), ImagePart(file_id="steer-image"))
    )

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                    primary=(TextPart("start"),),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=2)
            if interruption == "steer":
                handle.steer(steer, timing=timing)
            else:
                handle.cancel(timing=timing)
            gate.release()
            root = await asyncio.wait_for(handle, timeout=2)
            assert root.status == (
                "succeeded" if interruption == "steer" else "canceled"
            ), root.error
            if interruption == "steer":
                final = harness.adapter.invocations[-1].call.messages
                assert final.count(Message.user("start")) == 1
                assert final.count(steer_message(steer)) == 1
            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("scenario_name", ["execute", "child", "parallel", "repair"])
def test_execution_boundaries_keep_their_own_message_prefix(
    tmp_path: Path, scenario_name: str
) -> None:
    if scenario_name in {"execute", "child"}:
        action = "execute" if scenario_name == "execute" else "run"
        source = """
agic parent() -> Text:
  recall = none
  hands = agic:child
  handoffs = agic:child
  user: Parent.

agic child() -> Text:
  recall = none
  user: Child.
"""
        responses = [
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "child",
                        "child",
                        f"_toolang__{action}",
                        {"runnable": "agic:child"},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("child result")),
            ModelCallResult(message=Message.assistant("parent result")),
        ]
        runnable = "parent"
    elif scenario_name == "parallel":
        source = """
flow parent(_: Text) -> Text[]:
  storm 2 in 2 lanes using child

agic child(_: Text) -> Text:
  recall = none
  user: Child.
"""
        responses = [
            ModelCallResult(message=Message.assistant(str(i))) for i in range(3)
        ]
        runnable = "parent"
    else:
        source = """
agic parent() -> Boolean:
  recall = none
  user: Answer.
"""
        responses = [
            ModelCallResult(message=Message.assistant("invalid")),
            ModelCallResult(message=Message.assistant("true")),
        ]
        runnable = "parent"
    harness = ExecutionHarness.create(tmp_path, source=source, responses=responses)
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable=runnable,
                    primary=(TextPart("start"),)
                    if scenario_name == "parallel"
                    else None,
                ),
                tracer=tracer,
            )
            assert root.status == "succeeded", root.error

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_retry_deletes_its_deltas_and_can_reuse_step_ids(tmp_path: Path) -> None:
    tool = RecordingTool("lookup__item", output={"result": 1})
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[
            ModelCallResult(tool_calls=(ToolCall("old", "old", tool.name, {}),)),
            RuntimeError("model failed"),
            ModelCallResult(message=Message.assistant("new execution")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                    primary=(TextPart("start"),),
                )
            )
            assert root.status == "failed"
            old = harness.store.list_steps(run_id=root.id)
            retried = await harness.executor.retry(
                root.id, setup=harness.setup, state=harness.state, tracer=tracer
            )
            assert retried.status == "succeeded", retried.error
            saved = harness.store.list_steps(run_id=root.id)
            assert len(saved) == 1
            assert saved[0].ref == old[0].ref
            assert harness.store.get_step(ref=old[-1].ref) is None
            assert harness.adapter.invocations[-1].call.messages == [
                Message.user("start")
            ]

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_long_delta_sequences_are_linear_and_batch_expansion_is_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import toolang.execution.store as module

    store = RunStore(tmp_path / "runs.db")
    buffer = _MessageBuffer()
    try:
        steps = []
        total_messages = 1200
        for index in range(total_messages):
            buffer.append(Message.user(str(index)))
            steps.append(
                store.begin_step(
                    ref=StepRef.from_local("run_ab12", (index,)),
                    kind="model",
                    input=(),
                    state=ControlRef.for_run("run_ab12", 0),
                    started_at="now",
                    given=ModelStepGiven(
                        "test/model",
                        ModelCall("", list(buffer.messages)),
                        buffer.take_delta(),
                    ),
                )
            )
        assert all(
            isinstance(step.given, StoredModelStepGiven)
            and len(step.given.call.delta.messages) == 1
            for step in steps
        )
        assert (
            sum(
                len(str(delta_to_data(step.given.call.delta)))
                for step in steps
                if isinstance(step.given, StoredModelStepGiven)
            )
            < total_messages * 100
        )
        rendered = []
        original = module.render_delta

        def render(delta, resolve):
            rendered.append(delta)
            return original(delta, resolve)

        monkeypatch.setattr(module, "render_delta", render)
        calls = store.rebuild_model_calls((steps[2], steps[10], steps[-1]))
        assert len(rendered) == total_messages
        assert calls[steps[-1].ref].messages == buffer.messages
        assert calls[steps[10].ref].messages == buffer.messages[:11]
    finally:
        store.close()


def test_missing_message_dependency_fails_without_a_snapshot_fallback(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        missing = TypedRef(
            FieldRef.from_path(StepRef.parse("run_dead.0"), "output", "local", "value"),
            "Part[]",
        )
        step = store.begin_step(
            ref=StepRef.parse("run_ab12.0"),
            kind="model",
            input=(),
            state=ControlRef.for_run("run_ab12", 0),
            started_at="now",
            given=ModelStepGiven(
                "test/model",
                ModelCall("", []),
                MessageDelta(messages=(MessageTemplate("assistant", (missing,)),)),
            ),
        )
        with pytest.raises(ValueError, match="record not found: run_dead.0"):
            store.rebuild_model_call(step)
    finally:
        store.close()
