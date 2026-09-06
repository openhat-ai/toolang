"""Historical templates, horizons, and current deltas form exact model calls."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.execution_assertions import assert_replayed
from tests.support.execution_fixtures import project_run_start, project_run_end
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    RecordingTool,
)
from toolang.base.types.message import Message, TextPart, message_text
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.events import StepEnd
from toolang.execution.executor.executor import _Execution
from toolang.execution import assembly
from toolang.execution.records import (
    ControlRecord,
    RunControlPayload,
    StoredModelStepGiven,
)
from toolang.execution.types import (
    FieldRef,
    Local,
    RunRef,
    ThreadPrefix,
    TypedRef,
)
from toolang.state.prepare import prepare_agent_state


SOURCE = """
agic seed(_: Part[]) -> Part[]:
  context: none
  instruct: none
  user: {{_}}

agic chat(_: Part[]) -> Part[]:
  context: none
  instruct: none
  user: {{_}}
"""


def _summary(harness, thread, end, *, summary="Earlier facts.", begin=None):
    run = project_run_start(
        harness.store,
        run_id=harness.ids.issue_run(),
        thread_id=f"compact_{thread}",
        origin="test",
        input=Message.user("compact"),
    )
    project_run_end(
        harness.store,
        run_id=run.id,
        output=Local(
            {
                "thread": thread,
                "begin": begin,
                "end": end,
                "summary": summary,
            }
        ),
    )
    return FieldRef.from_path(RunRef(run.id), "output")


async def _run(harness, thread, text, tracer, *, runnable="seed", horizon=None):
    return await harness.executor.run(
        replace(
            harness.run_spec(
                thread=thread, runnable=runnable, primary=(TextPart(text),)
            ),
            horizon=horizon,
        ),
        tracer=tracer,
    )


def test_cross_run_deltas_record_only_new_messages(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ModelCallResult(message=Message.assistant(f"reply {i}")) for i in range(3)
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            runs = [await _run(harness, thread, f"input {i}", tracer) for i in range(3)]
            assert all(run.status == "succeeded" for run in runs)
            assert [inv.call.messages for inv in harness.adapter.invocations] == [
                [Message.user("input 0")],
                [
                    Message.user("input 0"),
                    Message.assistant("reply 0"),
                    Message.user("input 1"),
                ],
                [
                    Message.user("input 0"),
                    Message.assistant("reply 0"),
                    Message.user("input 1"),
                    Message.assistant("reply 1"),
                    Message.user("input 2"),
                ],
            ]
            steps = [harness.store.list_steps(run_id=run.id)[0] for run in runs]
            givens = [
                step.given
                for step in steps
                if isinstance(step.given, StoredModelStepGiven)
            ]
            assert [len(given.call.delta.messages) for given in givens] == [1, 2, 2]
            assert isinstance(givens[1].call.delta.messages[0].segments[0], TypedRef)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("compact", [False, True])
def test_history_keeps_consecutive_flow_outputs_without_child_internals(
    tmp_path: Path, compact: bool
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE
        + """
agic worker(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: Private worker input: {{_}}

flow job(_: Part[]) -> Part[]:
  run worker
""",
        responses=[
            ModelCallResult(message=Message.assistant(f"reply {i}")) for i in range(5)
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            await _run(harness, thread, "seed", tracer)
            first = await _run(harness, thread, "first job", tracer, runnable="job")
            await _run(harness, thread, "second job", tracer, runnable="job")
            horizon = _summary(harness, thread, first.id) if compact else None
            run = await _run(harness, thread, "chat", tracer, horizon=horizon)
            assert run.status == "succeeded", run.error
            expected = [
                *(
                    [Message.user("Earlier facts.")]
                    if compact
                    else [Message.user("seed"), Message.assistant("reply 0")]
                ),
                Message.user("first job"),
                Message.assistant("reply 1"),
                Message.user("second job"),
                Message.assistant("reply 2"),
                Message.user("chat"),
            ]
            assert harness.adapter.invocations[-1].call.messages == expected
            # Recorded tails stay in this delta; the following Run adds only
            # the latest reply and input, without duplicating the Flow outputs.
            following = await _run(harness, thread, "continue", tracer, horizon=horizon)
            assert following.status == "succeeded", following.error
            assert harness.adapter.invocations[-1].call.messages == [
                *expected,
                Message.assistant("reply 3"),
                Message.user("continue"),
            ]

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("action", ["execute", "retry"])
def test_execution_reset_uses_the_surviving_horizon(
    tmp_path: Path, action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = RecordingTool("lookup__item", output={})
    next_result = (
        ModelCallResult(
            tool_calls=(
                ToolCall("next", "next", "_too__execute", {"runnable": "agic:next"}),
            )
        )
        if action == "execute"
        else RuntimeError("injected model failure")
    )
    source = (
        SOURCE.replace(
            "agic chat(_: Part[]) -> Part[]:",
            "agic chat(_: Part[]) -> Part[]:\n  handoffs = agic:next",
        )
        + """
agic next() -> Part[]:
  context: none
  instruct: none
  user: Next task.
"""
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        tools={tool.name: tool},
        responses=[
            ModelCallResult(message=Message.assistant("first reply")),
            ModelCallResult(message=Message.assistant("second reply")),
            ModelCallResult(tool_calls=(ToolCall("lookup", "lookup", tool.name, {}),)),
            next_result,
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    tracer = RecordingRunTracer()
    compact: list[ControlRecord] = []
    horizon = None

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if isinstance(event, StepEnd) and event.kind == "tool" and not compact:
                assert horizon is not None
                compact.append(
                    harness.store.accept_compact_control(
                        run_id=event.step.run_id,
                        horizon=horizon,
                        triggered_by=event.step,
                        created_at=event.finished_at,
                    )
                )

    async def scenario():
        nonlocal horizon
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            await _run(harness, thread, "first", tracer)
            second = await _run(harness, thread, "second", tracer)
            horizon = _summary(harness, thread, second.id)
            run = await _run(harness, thread, "current", hooked, runnable="chat")
            if action == "retry":
                assert run.status == "failed"
                reads = []
                original = harness.store.run_horizon

                def read(run_id):
                    reads.append(len(harness.store.list_steps(run_id=run_id)))
                    return original(run_id)

                with monkeypatch.context() as patch:
                    patch.setattr(harness.store, "run_horizon", read)
                    run = await harness.executor.retry(
                        run.id, setup=harness.setup, state=harness.state, tracer=tracer
                    )
                assert reads == [0]
                # The compact-adopting Step was physically removed by retry.
                assert harness.store.run_horizon(run.id) is None
                assert not harness.store.runtime_controls(run_id=run.id)[1]
                expected = [
                    Message.user("first"),
                    Message.assistant("first reply"),
                    Message.user("second"),
                    Message.assistant("second reply"),
                    Message.user("current"),
                ]
            else:
                assert harness.store.run_horizon(run.id) == horizon
                expected = [
                    Message.user("Earlier facts."),
                    Message.assistant("first reply"),
                    Message.user("second"),
                    Message.assistant("second reply"),
                    Message.user("Next task."),
                ]
                tracer.events.extend(hooked.events)
            assert run.status == "succeeded", run.error
            assert harness.adapter.invocations[-1].call.messages == expected

    hooked = Tracer()
    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_compact_preparation_survives_failed_begin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ModelCallResult(message=Message.assistant(text))
            for text in ("first reply", "second reply", "done")
        ],
    )
    gate = AsyncGate()
    tracer = RecordingRunTracer()
    begin = _Execution.begin_step
    persist = harness.store.begin_step
    target = ""

    async def reprepare(execution, build):
        if execution._active.root_run_id == target and not gate.entered:
            build(*execution._current_state)
            await gate.wait()

            def fail_write(**kwargs):
                persist(**kwargs)
                raise RuntimeError("injected rollback")

            with monkeypatch.context() as patch:
                patch.setattr(harness.store, "begin_step", fail_write)
                with pytest.raises(RuntimeError, match="injected rollback"):
                    await begin(execution, build)
            assert execution.horizon_for(target) is None
            assert harness.store.list_steps(run_id=target) == []
            assert len(harness.adapter.invocations) == 2
        return await begin(execution, build)

    monkeypatch.setattr(_Execution, "begin_step", reprepare)

    async def scenario():
        nonlocal target
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            await _run(harness, thread, "first", tracer)
            second = await _run(harness, thread, "second", tracer)
            horizon = _summary(harness, thread, second.id)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="chat", primary=(TextPart("current"),)
                ),
                tracer=tracer,
            )
            target = handle.run_id
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            compact = harness.store.accept_compact_control(
                run_id=target,
                horizon=horizon,
                triggered_by=None,
                created_at="2026-09-06T00:00:00Z",
            )
            gate.release()
            run = await asyncio.wait_for(handle, 2)
            assert run.status == "succeeded", run.error
            (step,) = harness.store.list_steps(run_id=target)
            assert compact.ref in step.preceded_by
            assert harness.adapter.invocations[-1].call.messages[0] == Message.user(
                "Earlier facts."
            )
            assert harness.store.run_horizon(target) == horizon

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_context_is_reused_until_its_history_is_compacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = RecordingTool("lookup__item", output={})
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE.replace("  context: none\n", ""),
        tools={tool.name: tool},
        responses=[
            ModelCallResult(message=Message.assistant("first reply")),
            ModelCallResult(message=Message.assistant("second reply")),
            ModelCallResult(tool_calls=(ToolCall("lookup", "lookup", tool.name, {}),)),
            ModelCallResult(tool_calls=(ToolCall("again", "again", tool.name, {}),)),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    horizon: FieldRef | None = None
    compacted = False

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            nonlocal compacted
            await super().on_event(event)
            if isinstance(event, StepEnd) and event.kind == "tool" and not compacted:
                assert horizon is not None
                harness.store.accept_compact_control(
                    run_id=event.step.run_id,
                    horizon=horizon,
                    triggered_by=event.step,
                    created_at="2026-09-06T00:00:00Z",
                )
                compacted = True

    tracer = Tracer()
    reads = []
    renderings = []
    read = harness.store.list_steps_for_runs
    render = assembly.render_delta

    def read_steps(*, run_ids):
        reads.append(tuple(run_ids))
        return read(run_ids=run_ids)

    def render_history(delta, resolve):
        renderings.append(delta)
        return render(delta, resolve)

    async def scenario():
        nonlocal horizon
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            first = await _run(harness, thread, "first", tracer)
            second = await _run(harness, thread, "second", tracer)
            horizon = _summary(harness, thread, second.id)
            monkeypatch.setattr(harness.store, "list_steps_for_runs", read_steps)
            monkeypatch.setattr(assembly, "render_delta", render_history)
            run = await _run(harness, thread, "current", tracer, runnable="chat")
            assert run.status == "succeeded", run.error
            assert reads == [(first.id, second.id)]
            assert (
                len(renderings) == 3
            )  # two historical deltas and one newly consumed tail
            before, after, final = [
                item.call.messages for item in harness.adapter.invocations[-3:]
            ]
            assert (
                before[: len(harness.adapter.invocations[1].call.messages)]
                == harness.adapter.invocations[1].call.messages
            )
            for messages in (before, after, final):
                assert (
                    sum(
                        message_text(message.parts).count("<context>")
                        for message in messages
                    )
                    == 1
                )
            assert final[: len(after)] == after

    asyncio.run(scenario())
    monkeypatch.setattr(
        assembly,
        "control_message",
        lambda _control: pytest.fail("replay must use saved templates"),
    )
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("invalid", ["partial", "current", "step", "summary"])
def test_invalid_compact_coverage_never_dispatches(
    tmp_path: Path, invalid: str
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ModelCallResult(message=Message.assistant(text))
            for text in ("first", "second")
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            await _run(harness, thread, "first", tracer)
            second = await _run(harness, thread, "second", tracer)
            current = harness.ids.issue_run()
            horizon = _summary(
                harness,
                thread,
                current
                if invalid == "current"
                else f"{second.id}.0"
                if invalid == "step"
                else second.id,
                begin=second.id if invalid == "partial" else None,
                summary=42 if invalid == "summary" else "Earlier facts.",
            )
            run = await harness.executor.run(
                replace(
                    harness.run_spec(
                        thread=thread, runnable="chat", primary=(TextPart("current"),)
                    ),
                    horizon=horizon,
                ),
                run_id=current,
                tracer=tracer,
            )
            assert run.status == "failed"
            assert harness.store.list_steps(run_id=current) == []
            assert len(harness.adapter.invocations) == 2

    asyncio.run(scenario())


def test_far_and_near_are_available_to_templates_with_recall_none(
    tmp_path: Path,
) -> None:
    source = (
        SOURCE
        + """
agic describe() -> Text:
  recall = none
  context: none
  user: Summary: {{far}}; detail: {{near}}
"""
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[
            ModelCallResult(message=Message.assistant(text))
            for text in ("first reply", "second reply", "done")
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            await _run(harness, thread, "first", tracer)
            second = await _run(harness, thread, "second", tracer)
            horizon = _summary(harness, thread, second.id)
            run = await harness.executor.run(
                replace(
                    harness.run_spec(thread=thread, runnable="describe"),
                    horizon=horizon,
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded", run.error
            (message,) = harness.adapter.invocations[-1].call.messages
            text = message_text(message.parts)
            assert "Earlier facts." in text and "second" in text and "role" in text

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_fork_and_later_rewind_preserve_old_model_calls(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            ModelCallResult(message=Message.assistant(f"reply {i}")) for i in range(5)
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            await _run(harness, thread, "first", tracer)
            second = await _run(harness, thread, "second", tracer)
            fork = harness.threads.fork(thread_id=thread)
            harness.threads.rewind(thread_id=thread, run_id=second.id)
            await _run(harness, thread, "replacement", tracer)
            await _run(harness, fork, "forked", tracer)
            assert (
                Message.user("second") in harness.adapter.invocations[-1].call.messages
            )
            assert (
                Message.user("replacement")
                not in harness.adapter.invocations[-1].call.messages
            )
            harness.threads.rewind(thread_id=fork, run_id=second.id)
            await _run(harness, fork, "new fork tail", tracer)
            assert (
                Message.user("second")
                not in harness.adapter.invocations[-1].call.messages
            )

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_reload_captures_recall_without_reading_state_during_replay(
    tmp_path: Path,
) -> None:
    gate = AsyncGate()
    tool = RecordingTool("lookup__item", output={}, gate=gate)
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[
            ModelCallResult(message=Message.assistant("first reply")),
            ModelCallResult(tool_calls=(ToolCall("lookup", "lookup", tool.name, {}),)),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            await _run(harness, thread, "first", tracer)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="chat", primary=(TextPart("current"),)
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            layout = harness.setup.layout
            layout.program.write_text(
                SOURCE.replace(
                    "agic chat(_: Part[]) -> Part[]:",
                    "agic chat(_: Part[]) -> Part[]:\n  recall = none",
                ),
                encoding="utf-8",
            )
            state = prepare_agent_state(layout)
            reload = handle.reload(state)
            await asyncio.wait_for(
                harness.executor._wait_for_control(
                    harness.executor._active[handle.run_id], reload
                ),
                2,
            )
            gate.release()
            run = await asyncio.wait_for(handle, 2)
            assert run.status == "succeeded", run.error
            before, after = [
                item.call.messages for item in harness.adapter.invocations[-2:]
            ]
            assert before == [
                Message.user("first"),
                Message.assistant("first reply"),
                Message.user("current"),
            ]
            # The late tail was first recorded in this Run; reload preserves now.
            assert after[:2] == before[1:]
            givens = [
                s.given
                for s in harness.store.list_steps(run_id=run.id)
                if isinstance(s.given, StoredModelStepGiven)
            ]
            assert [g.call.recall for g in givens] == [("far", "near"), ("none",)]

    asyncio.run(scenario())
    for index, path in enumerate(
        (
            harness.setup.layout.root_state,
            harness.setup.layout.home_state,
            harness.setup.layout.agent_state,
        )
    ):
        path.rename(tmp_path / f"unavailable-state-{index}")
    assert_replayed(harness.store.db_path, tracer.events)


def test_parent_compact_does_not_change_active_child(tmp_path: Path) -> None:
    source = (
        SOURCE
        + """
agic parent(_: Part[]) -> Part[]:
  hands = agic:child
  context: none
  user: {{_}}

agic child() -> Text:
  context: none
  user: Child.
"""
    )
    gate = AsyncGate()
    tool = RecordingTool("lookup__item", output={}, gate=gate)

    def child_call(name):
        return ModelCallResult(
            tool_calls=(ToolCall(name, name, "_too__run", {"runnable": "agic:child"}),)
        )

    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        tools={tool.name: tool},
        responses=[
            ModelCallResult(message=Message.assistant("first reply")),
            ModelCallResult(message=Message.assistant("second reply")),
            child_call("first-child"),
            ModelCallResult(tool_calls=(ToolCall("lookup", "lookup", tool.name, {}),)),
            ModelCallResult(message=Message.assistant("child reply")),
            child_call("second-child"),
            ModelCallResult(message=Message.assistant("next child reply")),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            await _run(harness, thread, "first", tracer)
            second = await _run(harness, thread, "second", tracer)
            old = _summary(harness, thread, second.id, summary="Old far.")
            new = _summary(harness, thread, second.id, summary="New far.")
            handle = harness.executor.run(
                replace(
                    harness.run_spec(
                        thread=thread, runnable="parent", primary=(TextPart("current"),)
                    ),
                    horizon=old,
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            compact = harness.store.accept_compact_control(
                run_id=handle.run_id,
                horizon=new,
                triggered_by=None,
                created_at="2026-09-06T00:00:00Z",
            )
            gate.release()
            run = await asyncio.wait_for(handle, 2)
            assert run.status == "succeeded", run.error
            children = [
                r
                for r in harness.store.list_run_tree(root_run_id=run.id)
                if r.parent is not None
            ]
            assert len(children) == 2
            horizons = []
            for child in children:
                control = harness.store.get_run_control(run_id=child.id, index=0)
                assert control is not None and isinstance(
                    control.payload, RunControlPayload
                )
                horizons.append(control.payload.horizon)
                assert all(
                    compact.ref not in step.preceded_by
                    for step in harness.store.list_steps(run_id=child.id)
                )
            assert horizons == [old, new]
            calls = [item.call for item in harness.adapter.invocations[2:]]
            assert [call.messages[0] for call in calls] == [
                Message.user(text)
                for text in (
                    "Old far.",
                    "Old far.",
                    "Old far.",
                    "New far.",
                    "New far.",
                    "New far.",
                )
            ]

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("recall", [None, "auto", "none", "far", "near", "far, near"])
def test_initial_horizon_and_recall_selection(
    tmp_path: Path, recall: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = SOURCE.replace(
        "agic chat(_: Part[]) -> Part[]:",
        "agic chat(_: Part[]) -> Part[]:"
        + (f"\n  recall = {recall}" if recall else ""),
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[
            ModelCallResult(message=Message.assistant(f"reply {i}")) for i in range(3)
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            first = await _run(harness, thread, "first", tracer)
            second = await _run(harness, thread, "second", tracer)
            horizon = _summary(
                harness, thread, second.id, begin=first.id if recall == "auto" else None
            )
            reads = []
            original = harness.store.list_steps_for_runs

            def read(*, run_ids):
                reads.append(tuple(run_ids))
                return original(run_ids=run_ids)

            with monkeypatch.context() as patch:
                patch.setattr(harness.store, "list_steps_for_runs", read)
                run = await _run(
                    harness, thread, "current", tracer, runnable="chat", horizon=horizon
                )
            # Even explicit template inputs need only the uncompressed prefix.
            assert reads == [(second.id,)]
            assert run.status == "succeeded", run.error
            selected = (
                ("far", "near")
                if recall in {None, "auto"}
                else tuple(recall.split(", "))
            )
            assert harness.adapter.invocations[-1].call.messages == [
                *([Message.user("Earlier facts.")] if "far" in selected else []),
                *(
                    [
                        Message.assistant("reply 0"),
                        Message.user("second"),
                        Message.assistant("reply 1"),
                    ]
                    if "near" in selected
                    else []
                ),
                Message.user("current"),
            ]
            (step,) = harness.store.list_steps(run_id=run.id)
            assert isinstance(step.given, StoredModelStepGiven)
            assert step.given.call.recall == selected
            assert len(step.given.call.delta.messages) == (
                2 if "near" in selected else 1
            )
            entry = harness.store.get_run_control(run_id=run.id, index=0)
            assert entry is not None and isinstance(entry.payload, RunControlPayload)
            assert entry.payload.horizon == horizon
            assert first.id not in message_text(
                harness.adapter.invocations[-1].call.messages[0].parts
            )

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_compact_adoption_replaces_history_and_preserves_now(tmp_path: Path) -> None:
    tool = RecordingTool("lookup__item", output={"value": 1})
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[
            ModelCallResult(message=Message.assistant("first reply")),
            ModelCallResult(message=Message.assistant("second reply")),
            ModelCallResult(tool_calls=(ToolCall("lookup", "lookup", tool.name, {}),)),
            ModelCallResult(message=Message.assistant("done")),
            ModelCallResult(message=Message.assistant("rerun done")),
        ],
    )
    horizon: FieldRef | None = None
    compact: ControlRecord | None = None

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            nonlocal compact
            await super().on_event(event)
            if isinstance(event, StepEnd) and event.kind == "tool":
                assert horizon is not None
                compact = harness.store.accept_compact_control(
                    run_id=event.step.run_id,
                    horizon=horizon,
                    triggered_by=event.step,
                    created_at="2026-09-06T00:00:00Z",
                )

    tracer = Tracer()

    async def scenario():
        nonlocal horizon
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            await _run(harness, thread, "first", tracer)
            second = await _run(harness, thread, "second", tracer)
            horizon = _summary(harness, thread, second.id)
            run = await _run(harness, thread, "current", tracer, runnable="chat")
            assert run.status == "succeeded", run.error
            before, after = [
                item.call.messages for item in harness.adapter.invocations[-2:]
            ]
            assert before[0] == Message.user("first")
            assert after[:5] == [Message.user("Earlier facts."), *before[1:]]
            assert [message.role for message in after[5:]] == ["assistant", "tool"]
            models = [
                s
                for s in harness.store.list_steps(run_id=run.id)
                if isinstance(s.given, StoredModelStepGiven)
            ]
            assert compact is not None
            assert models[-1].preceded_by == (compact.ref,)
            assert [
                len(s.given.call.delta.messages)
                for s in models
                if isinstance(s.given, StoredModelStepGiven)
            ] == [2, 2]
            assert harness.store.run_horizon(run.id) == horizon
            rerun = await harness.executor.rerun(
                run.id, setup=harness.setup, state=harness.state, tracer=tracer
            )
            assert rerun.status == "succeeded", rerun.error
            entry = harness.store.get_run_control(run_id=rerun.id, index=0)
            assert entry is not None and isinstance(entry.payload, RunControlPayload)
            assert entry.payload.horizon == horizon
            assert harness.adapter.invocations[-1].call.messages[0] == Message.user(
                "Earlier facts."
            )

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)
