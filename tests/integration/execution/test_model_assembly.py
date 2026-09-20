"""Historical templates, horizons, and current deltas form exact model calls."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.execution_assertions import (
    assert_replayed,
    without_route_snapshots,
)
from tests.support.execution_fixtures import (
    accept_run,
    project_run_end,
)
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    RecordingTool,
)
from toolang.base.types.message import Message, TextPart, message_text
from toolang.base.types.model import Model, ModelRoute, ModelToolang
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.plugin.adapters.responses import response_payload
from toolang.execution.events import StepEnd
from toolang.execution.executor.executor import _Execution
from toolang.execution.assembly import history as execution_history
from toolang.execution.assembly.utils import render_delta
from toolang.execution.records import (
    ControlRecord,
    RunControlPayload,
    StoredModelStepGiven,
)
from toolang.execution.types import (
    Output,
    ContentRef,
    FieldRef,
    Local,
    RunRef,
    ThreadPrefix,
)
from toolang.state.prepare import prepare_agent_state
from toolang.lang.input import RunnableInput
from toolang.common.time import utc_now


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
    # Keep explicit adoption independent of automatic compact-thread discovery.
    summary_thread = f"summary_{thread}"
    if harness.store.get_thread(thread_id=summary_thread) is None:
        harness.store.create_thread(
            thread_id=summary_thread, origin="test", created_at=utc_now()
        )
    run, _ = accept_run(
        harness.store,
        run_id=harness.ids.issue_run(),
        parent=None,
        thread=summary_thread,
        input=RunnableInput({"thread": thread, "begin": begin, "end": end}),
        context={},
        request_id=None,
        created_at=utc_now(),
    )
    project_run_end(
        harness.store,
        run_id=run.id,
        output=Output(
            Local({"thread": thread, "begin": begin, "end": end, "summary": summary}),
            None,
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


@pytest.mark.parametrize(
    "declarations,selection,expected",
    [
        pytest.param("", "", "model_provider: test", id="bundled"),
        pytest.param(
            "context: Private context for {{agent.name}}.\n",
            "",
            "Private context for alice.",
            id="program-default",
        ),
        pytest.param(
            "context: Private context for {{agent.name}}.\n",
            "  context: default\n",
            "Private context for alice.",
            id="explicit-default",
        ),
        pytest.param(
            "context: Unselected context.\ncontext report: Private context for {{agent.name}}.\n",
            "  context: report\n",
            "Private context for alice.",
            id="named",
        ),
        pytest.param(
            "context: Unselected context.\n",
            "  context:\n    Private context for {{agent.name}}.\n",
            "Private context for alice.",
            id="inline",
        ),
        pytest.param(
            "context: Unselected context.\n",
            "  context: none\n",
            None,
            id="none",
        ),
    ],
)
def test_context_selection_keeps_data_and_current_input_out_of_instructions(
    tmp_path: Path, declarations, selection, expected
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=(
            declarations
            + "instruct: Agent behavior.\n"
            + "agic chat(_: Text) -> Text:\n"
            + selection
            + "  user: {{_}}\n"
        ),
        responses=[ModelCallResult(message=Message.assistant("done"))],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            run = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="chat",
                    primary=(TextPart("Current user objective."),),
                ),
                tracer=tracer,
            )
            assert run.status == "succeeded", run.error
            (invocation,) = harness.adapter.invocations
            call = invocation.call
            assert call.instructions.startswith("<toolang:protocol>")
            assert (
                "<toolang:instruct>\nAgent behavior.\n</toolang:instruct>"
                in call.instructions
            )
            assert "Current user objective." not in call.instructions
            assert "Unselected context." not in call.instructions
            (message,) = without_route_snapshots(call.messages)
            assert message.role == "user"
            text = message_text(message.parts)
            assert text.endswith("Current user objective.")
            assert text.count("Current user objective.") == 1
            assert "Agent behavior." not in text and "Unselected context." not in text
            if expected is None:
                assert message == Message.user("Current user objective.")
            else:
                assert expected not in call.instructions
                assert text.count(expected) == 1
                assert text.startswith("<toolang:context>\n")
                assert (
                    text.count("<toolang:context>")
                    == text.count("</toolang:context>")
                    == 1
                )
                assert text.endswith("</toolang:context>\n\nCurrent user objective.")

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_cross_run_baselines_do_not_duplicate_historical_contributions(
    tmp_path: Path,
) -> None:
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
            assert [
                without_route_snapshots(inv.call.messages)
                for inv in harness.adapter.invocations
            ] == [
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
            assert [len(given.call.messages.delta) for given in givens] == [1, 3, 5]
            assert [
                sum(m.source is None for m in g.call.messages.delta) for g in givens
            ] == [1, 2, 2]
            assert isinstance(givens[1].call.messages.delta[0].content[0], ContentRef)
            assert (
                givens[1].call.messages.delta[0].content
                == givens[0].call.messages.delta[0].content
            )
            assert all(
                g.call.messages.head == s.ref
                for g, s in zip(givens, steps, strict=True)
            )

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
            assert (
                without_route_snapshots(harness.adapter.invocations[-1].call.messages)
                == expected
            )
            # Recorded tails stay in this delta; the following Run adds only
            # the latest reply and input, without duplicating the Flow outputs.
            following = await _run(harness, thread, "continue", tracer, horizon=horizon)
            assert following.status == "succeeded", following.error
            assert without_route_snapshots(
                harness.adapter.invocations[-1].call.messages
            ) == [
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
                ToolCall(
                    "next", "next", "_toolang__execute", {"runnable": "agic:next"}
                ),
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
            assert (
                without_route_snapshots(harness.adapter.invocations[-1].call.messages)
                == expected
            )

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
            assert without_route_snapshots(
                harness.adapter.invocations[-1].call.messages
            )[0] == Message.user("Earlier facts.")
            assert harness.store.run_horizon(target) == horizon

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("context", [None, "Plain runtime context."])
def test_each_call_records_context_without_rerendering_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, context: str | None
) -> None:
    tool = RecordingTool("lookup__item", output={})
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE.replace(
            "  context: none\n", f"  context: {context}\n" if context else ""
        ),
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
    render = execution_history.render_delta

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
            monkeypatch.setattr(execution_history, "render_delta", render_history)
            run = await _run(harness, thread, "current", tracer, runnable="chat")
            assert run.status == "succeeded", run.error
            assert reads == [(first.id, second.id)]
            assert (
                len(renderings) == 3
            )  # two historical deltas and one newly consumed tail
            before, after, final = [
                without_route_snapshots(item.call.messages)
                for item in harness.adapter.invocations[-3:]
            ]
            assert before[
                : len(
                    without_route_snapshots(
                        harness.adapter.invocations[1].call.messages
                    )
                )
            ] == without_route_snapshots(harness.adapter.invocations[1].call.messages)
            for messages, count in ((before, 3), (after, 3), (final, 4)):
                assert (
                    sum(
                        message_text(message.parts).count(
                            context or "<toolang:context>"
                        )
                        for message in messages
                    )
                    == count
                )
            models = [
                step
                for step in harness.store.list_steps(run_id=run.id)
                if isinstance(step.given, StoredModelStepGiven)
            ]
            for step, expected in zip(models, (1, 2, 1), strict=True):
                if isinstance(step.given, StoredModelStepGiven):
                    assert (
                        sum(
                            message_text(message.parts).count(
                                context or "<toolang:context>"
                            )
                            for message in render_delta(
                                tuple(
                                    m
                                    for m in step.given.call.messages.delta
                                    if m.source is None
                                ),
                                harness.store.resolve_value,
                            )
                        )
                        == expected
                    )
            assert final[: len(after)] == after

    asyncio.run(scenario())
    monkeypatch.setattr(
        execution_history,
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
            (message,) = without_route_snapshots(
                harness.adapter.invocations[-1].call.messages
            )
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
            assert Message.user("second") in without_route_snapshots(
                harness.adapter.invocations[-1].call.messages
            )
            assert Message.user("replacement") not in without_route_snapshots(
                harness.adapter.invocations[-1].call.messages
            )
            harness.threads.rewind(thread_id=fork, run_id=second.id)
            await _run(harness, fork, "new fork tail", tracer)
            assert Message.user("second") not in without_route_snapshots(
                harness.adapter.invocations[-1].call.messages
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
                without_route_snapshots(item.call.messages)
                for item in harness.adapter.invocations[-2:]
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
            assert len({g.call.messages.head for g in givens}) == 2

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
            tool_calls=(
                ToolCall(name, name, "_toolang__run", {"runnable": "agic:child"}),
            )
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
            assert [without_route_snapshots(call.messages)[0] for call in calls] == [
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
            assert without_route_snapshots(
                harness.adapter.invocations[-1].call.messages
            ) == [
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
            assert step.given.call.messages.head == step.ref
            assert len(step.given.call.messages.delta) == len(
                without_route_snapshots(harness.adapter.invocations[-1].call.messages)
            )
            entry = harness.store.get_run_control(run_id=run.id, index=0)
            assert entry is not None and isinstance(entry.payload, RunControlPayload)
            assert entry.payload.horizon == horizon
            assert first.id not in message_text(
                without_route_snapshots(harness.adapter.invocations[-1].call.messages)[
                    0
                ].parts
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
                without_route_snapshots(item.call.messages)
                for item in harness.adapter.invocations[-2:]
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
                len(s.given.call.messages.delta)
                for s in models
                if isinstance(s.given, StoredModelStepGiven)
            ] == [len(item.call.messages) for item in harness.adapter.invocations[-2:]]
            assert all(
                s.given.call.messages.head == s.ref
                for s in models
                if isinstance(s.given, StoredModelStepGiven)
            )
            assert harness.store.run_horizon(run.id) == horizon
            rerun = await harness.executor.rerun(
                run.id, setup=harness.setup, state=harness.state, tracer=tracer
            )
            assert rerun.status == "succeeded", rerun.error
            entry = harness.store.get_run_control(run_id=rerun.id, index=0)
            assert entry is not None and isinstance(entry.payload, RunControlPayload)
            assert entry.payload.horizon == horizon
            assert without_route_snapshots(
                harness.adapter.invocations[-1].call.messages
            )[0] == Message.user("Earlier facts.")

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_compaction_between_tools_resets_the_last_model_baseline(tmp_path):
    tool = RecordingTool("lookup__item", output={"value": 1})
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[
            ModelCallResult(message=Message.assistant("old reply")),
            ModelCallResult(message=Message.assistant("recent reply")),
            ModelCallResult(
                tool_calls=tuple(
                    ToolCall(f"lookup{i}", f"lookup{i}", tool.name, {})
                    for i in range(2)
                ),
                continuation={
                    "previous_response_id": "uncompacted",
                    "reasoning": {
                        "lookup0": [{"id": "rs_0", "type": "reasoning", "summary": []}],
                    },
                },
            ),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )
    horizon = None
    adopted = False

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            nonlocal adopted
            await super().on_event(event)
            if isinstance(event, StepEnd) and event.kind == "tool" and not adopted:
                assert horizon is not None
                harness.store.accept_compact_control(
                    run_id=event.step.run_id,
                    horizon=horizon,
                    triggered_by=event.step,
                    created_at=event.finished_at,
                )
                adopted = True

    tracer = Tracer()

    async def scenario():
        nonlocal horizon
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            await _run(harness, thread, "old input", tracer)
            recent = await _run(harness, thread, "recent input", tracer)
            horizon = _summary(harness, thread, recent.id)
            run = await _run(harness, thread, "current", tracer)
            assert run.status == "succeeded", run.error
            request = harness.adapter.invocations[-1].call
            payload = response_payload(
                Model(
                    id="model",
                    name="model",
                    _toolang=ModelToolang(
                        provider="openai",
                        ready=True,
                        route=ModelRoute(adapter="responses"),
                    ),
                ),
                request,
                stateful=True,
            )
            assert "previous_response_id" not in payload
            assert [
                item["call_id"]
                for item in payload["input"]
                if item["type"] == "function_call_output"
            ] == ["lookup0", "lookup1"]
            assert [
                item["call_id"]
                for item in payload["input"]
                if item["type"] == "function_call"
            ] == ["lookup0", "lookup1"]
            assert [
                item["id"] for item in payload["input"] if item["type"] == "reasoning"
            ] == ["rs_0"]
            assert request.messages[0] == Message.user("Earlier facts.")
            assert "old input" not in str(request.messages)
            assert len(tool.calls) == 2
            step = harness.store.list_steps(run_id=run.id)[-1]
            assert isinstance(step.given, StoredModelStepGiven)
            assert step.given.call.messages.head == step.ref
            assert harness.store.rebuild_model_call(step) == request

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)
