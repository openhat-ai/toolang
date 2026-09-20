"""Online message assembly and durable replay use the same delta semantics."""

from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from tests.support.execution_assertions import (
    assert_run_event_integrity,
    steer_message,
    assert_replayed,
    without_route_snapshots,
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
    MessageRecall,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    message_text,
)
from toolang.base.types.run import ModelCall, ModelCallResult, ToolCall
from toolang.common.layout import AgentLayout
from toolang.execution.events import PartBegin, PartEnd, RunEvent, StepBegin, StepEnd
from toolang.execution.assembly.message_buffer import MessageBuffer
from toolang.execution.inspection.history import RunHistory
from toolang.execution.records import delta_to_data
from toolang.execution.recall import recall_revisions
from toolang.execution.values import parts_from_local
from toolang.execution.records import RecallControlPayload, StoredModelStepGiven
from toolang.execution.store import RunStore
from toolang.execution.types import (
    ControlTiming,
    FieldRef,
    ModelMessages,
    ContentRef,
    MessageTemplate,
    ControlRef,
    ModelStepGiven,
    RecallTarget,
    RulesRecallTarget,
    ServiceRecallTarget,
    SkillRecallTarget,
    SkillTriggerRecallTarget,
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
                len(step.given.call.messages.delta)
                for step in model_steps
                if isinstance(step.given, StoredModelStepGiven)
            ] == [1, *([3] * 12)]
            for step in model_steps[1:]:
                assert isinstance(step.given, StoredModelStepGiven)
                assert all(
                    isinstance(message.content[0], ContentRef)
                    for message in step.given.call.messages.delta
                )
            historical_outputs = {step.ref for step in steps[:-1]}
            assert not any(
                isinstance(value, TypedRef) and value.ref.record in historical_outputs
                for value in resolved
            )
            assert [
                len(item.call.messages) for item in harness.adapter.invocations
            ] == list(range(1, 38, 3))

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
                assert without_route_snapshots(
                    harness.adapter.invocations[-1].call.messages
                ) == [
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
                final = without_route_snapshots(
                    harness.adapter.invocations[-1].call.messages
                )
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
            assert without_route_snapshots(
                harness.adapter.invocations[-1].call.messages
            ) == [Message.user("start")]

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_long_delta_sequences_are_linear_and_batch_expansion_is_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import toolang.execution.store as module

    store = RunStore(tmp_path / "runs.db")
    buffer = MessageBuffer()
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
                        buffer.take_delta(StepRef.from_local("run_ab12", (index,))),
                        setup="test-setup",
                    ),
                )
            )
        assert all(
            isinstance(step.given, StoredModelStepGiven)
            and len(step.given.call.messages.delta) == 1
            for step in steps
        )
        assert (
            sum(
                len(str(delta_to_data(step.given.call.messages.delta)))
                for step in steps
                if isinstance(step.given, StoredModelStepGiven)
            )
            < total_messages * 200
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


def test_delta_cannot_exceed_the_assembled_call(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        missing = TypedRef(
            FieldRef.from_path(StepRef.parse("run_dead.0"), "output", "local", "value"),
            "Part[]",
        )
        with pytest.raises(ValueError, match="delta exceeds the assembled messages"):
            store.begin_step(
                ref=StepRef.parse("run_ab12.0"),
                kind="model",
                input=(),
                state=ControlRef.for_run("run_ab12", 0),
                started_at="now",
                given=ModelStepGiven(
                    "test/model",
                    ModelCall("", []),
                    ModelMessages(
                        StepRef.parse("run_ab12.0"),
                        (MessageTemplate("assistant", (missing,)),),
                    ),
                    setup="test-setup",
                ),
            )
    finally:
        store.close()


def _record(store, index, call, messages=None):
    ref = StepRef.from_local("run_ab12", (index,))
    return store.begin_step(
        ref=ref,
        kind="model",
        input=(),
        state=ControlRef.for_run(ref.run_id, 0),
        started_at="now",
        given=ModelStepGiven("test/model", call, messages, setup="test-setup"),
    )


@pytest.mark.parametrize("continuation", [False, True])
def test_batch_replay_does_not_read_unrequested_sequence_or_suffix(
    tmp_path,
    continuation,
) -> None:
    with closing(RunStore(tmp_path / "runs.db")) as store:
        first = _record(store, 0, ModelCall("", [Message.user("first")]))
        skipped = _record(
            store,
            1,
            ModelCall("", [Message.user("first"), Message.user("unneeded")])
            if continuation
            else ModelCall("", [Message.user("unneeded")]),
            ModelMessages(first.ref, (MessageTemplate("user", ("unneeded",)),))
            if continuation
            else None,
        )
        last = _record(store, 10, ModelCall("", [Message.user("new baseline")]))
        assert isinstance(skipped.given, StoredModelStepGiven)
        ref = skipped.given.call.messages.delta[-1].content[0]
        with sqlite3.connect(store.db_path) as connection:
            connection.execute("DELETE FROM contents WHERE id = ?", (str(ref),))
        calls = store.rebuild_model_calls((last, first))
        assert calls[first.ref].messages == [Message.user("first")]
        assert calls[last.ref].messages == [Message.user("new baseline")]
        with pytest.raises(ValueError, match="content is missing"):
            store.rebuild_model_call(skipped)


def test_conflicting_model_begin_rolls_back_new_content(tmp_path) -> None:
    with closing(RunStore(tmp_path / "runs.db")) as store:
        original = ModelCall("original instructions", [Message.user("original")])
        first = _record(store, 0, original)
        with sqlite3.connect(store.db_path) as connection:
            before = connection.execute(
                "SELECT id, value FROM contents ORDER BY id"
            ).fetchall()
        with pytest.raises(ValueError, match="conflicting step_begin event"):
            _record(
                store,
                0,
                ModelCall("different instructions", [Message.user("different")]),
            )
        with sqlite3.connect(store.db_path) as connection:
            assert (
                connection.execute(
                    "SELECT id, value FROM contents ORDER BY id"
                ).fetchall()
                == before
            )
        assert store.rebuild_model_call(first) == original
        assert _record(store, 0, original) == first


@pytest.mark.parametrize("head", ["run_ab12.1", "run_ab12.9", "run_other.0"])
def test_replay_rejects_disconnected_intermediate_message_heads(tmp_path, head):
    with closing(RunStore(tmp_path / "runs.db")) as store:
        first = _record(store, 0, ModelCall("", [Message.user("first")]))
        middle = _record(
            store,
            1,
            ModelCall("", [Message.user("first"), Message.user("middle")]),
            ModelMessages(first.ref, (MessageTemplate("user", ("middle",)),)),
        )
        last = _record(
            store,
            2,
            ModelCall("", [Message.user(text) for text in ("first", "middle", "last")]),
            ModelMessages(first.ref, (MessageTemplate("user", ("last",)),)),
        )
        with sqlite3.connect(store.db_path) as connection:
            connection.execute(
                "UPDATE steps SET given = json_set(given, '$.call.messages.head', ?) "
                "WHERE id = ?",
                (head, str(middle.ref)),
            )

        with pytest.raises(ValueError, match="model message head"):
            store.rebuild_model_call(last)
        with pytest.raises(ValueError, match="model message head"):
            store.rebuild_model_calls((first, last))
        assert store.rebuild_model_call(first).messages == [Message.user("first")]


def test_replayed_withdrawal_preserves_old_calls_and_trigger_visibility(
    tmp_path,
) -> None:
    path = tmp_path / "runs.db"
    revision = "a" * 64
    trigger, guidance, removed = (
        Message(
            "user",
            (TextPart(text),),
            tag=tag,
            recall=MessageRecall("skill/testing", rev),
        )
        for tag, rev, text in (
            ("skill-trigger", revision, "Use for testing."),
            ("skill-guidance", revision, "Run the deterministic tests."),
            (
                "skill-guidance",
                "0",
                '<toolang:skill-guidance ref="skill/testing" removed="true"/>',
            ),
        )
    )
    with closing(RunStore(path)) as store:
        first = _record(store, 0, ModelCall("", [trigger, guidance]))
        buffer = MessageBuffer((trigger, guidance))
        buffer.take_delta(first.ref)
        buffer.append(removed)
        second = _record(
            store,
            1,
            ModelCall("", list(buffer.messages)),
            buffer.take_delta(StepRef.parse("run_ab12.1")),
        )
        assert isinstance(second.given, StoredModelStepGiven)
        (delta,) = second.given.call.messages.delta
        assert delta.tag == "skill-guidance" and delta.recall == removed.recall
    with closing(RunStore(path)) as store:
        before, after = (
            store.rebuild_model_call(step).messages for step in (first, second)
        )
        assert [(m.tag, m.recall) for m in before] == [
            (trigger.tag, trigger.recall),
            (guidance.tag, guidance.recall),
        ]
        assert recall_revisions(before) == {
            SkillTriggerRecallTarget("skill/testing"): revision,
            SkillRecallTarget("skill/testing"): revision,
        }
        assert recall_revisions(after) == {
            SkillTriggerRecallTarget("skill/testing"): revision,
            SkillRecallTarget("skill/testing"): "0",
        }


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_recorded_messages_freeze_actual_parts_and_require_intact_hashes(
    tmp_path: Path,
    monkeypatch,
    damage,
) -> None:
    path = tmp_path / "runs.db"
    store = RunStore(path)
    original = Message.user("Actually sent {{literal}}")
    head = StepRef.parse("run_ab12.0")
    # Persistence must freeze the already assembled call, not reread this field.
    unavailable = TypedRef(FieldRef.from_path(head, "output", "local", "value"), "Text")
    monkeypatch.setattr(
        store,
        "resolve_value",
        lambda _: pytest.fail("capture must not resolve messages again"),
    )
    step = _record(
        store,
        0,
        ModelCall("instructions", [original]),
        ModelMessages(head, (MessageTemplate("user", (unavailable,)),)),
    )
    assert isinstance(step.given, StoredModelStepGiven)
    content = step.given.call.messages.delta[0].content[0]
    assert isinstance(content, ContentRef)
    store.close()
    with closing(RunStore(path)) as reopened:
        assert reopened.rebuild_model_call(step).messages == [original]
    with sqlite3.connect(path) as connection:
        if damage == "missing":
            connection.execute("DELETE FROM contents WHERE id = ?", (str(content),))
        else:
            connection.execute(
                "UPDATE contents SET value = ? WHERE id = ?", (b"corrupt", str(content))
            )
    with closing(RunStore(path)) as reopened:
        with pytest.raises(ValueError, match="missing|corrupted"):
            reopened.rebuild_model_call(step)


def test_heads_rebase_messages_but_never_inherit_other_call_fields(
    tmp_path, monkeypatch
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        first = _record(
            store,
            0,
            ModelCall(
                "before",
                [Message.user("old")],
                output_schema={"type": "string"},
                continuation={"id": "old"},
            ),
        )
        current = _record(
            store,
            10,
            ModelCall("after", [Message.user("summary"), Message.user("now")]),
        )
        assert isinstance(current.given, StoredModelStepGiven)
        head = current.ref
        next_call = ModelCall(
            "last",
            [Message.user("summary"), Message.user("now"), Message.user("next")],
            max_output_tokens=200,
        )
        final = _record(
            store,
            11,
            next_call,
            ModelMessages(head, (MessageTemplate("user", ("next",)),)),
        )
        resolved = []
        resolve = store.resolve_value
        monkeypatch.setattr(
            store, "resolve_value", lambda ref: (resolved.append(ref), resolve(ref))[1]
        )
        rebuilt = store.rebuild_model_call(final)
        assert rebuilt == next_call
        assert rebuilt.output_schema is None and rebuilt.continuation is None
        assert isinstance(first.given, StoredModelStepGiven)
        assert first.given.call.messages.delta[0].content[0] not in resolved
        assert store.rebuild_model_call(first).messages == [Message.user("old")]
        for bad_head in (
            first.ref,
            StepRef.parse("run_dead.10"),
            StepRef.parse("run_ab12.15"),
        ):
            with pytest.raises(ValueError, match="head"):
                _record(store, 12, next_call, ModelMessages(bad_head))
        with pytest.raises(ValueError, match="complete baseline"):
            _record(store, 12, next_call, ModelMessages(StepRef.parse("run_ab12.12")))
        bad = replace(
            final,
            given=replace(
                final.given,
                call=replace(
                    final.given.call,
                    messages=ModelMessages(StepRef.parse("run_ab12.9")),
                ),
            ),
        )
        # Corrupt persisted heads fail closed rather than guessing from controls.
        from toolang.execution.records import stored_step_given_to_data
        import json

        with sqlite3.connect(store.db_path) as connection:
            connection.execute(
                "UPDATE steps SET given = ? WHERE id = ?",
                (
                    json.dumps(stored_step_given_to_data("model", bad.given)),
                    str(final.ref),
                ),
            )
        with pytest.raises(ValueError, match="head"):
            store.rebuild_model_call(bad)
    finally:
        store.close()
