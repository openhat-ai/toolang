"""Automatic compaction stays outside the normal conversation and is replayable."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path

import pytest

from tests.support.execution_assertions import assert_replayed
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.history import RunHistory
from toolang.execution.executor.compact import permit
from toolang.execution.records import CompactControlPayload, RunControlPayload
from toolang.execution.types import ThreadPrefix, ToolStepGiven
from toolang.plugin.models.collections import ModelCollection


SOURCE = """agic chat(_: Part[]) -> Text:
  context: none
  instruct: none
  user: {{_}}
"""


def spec(harness, thread, text):
    return harness.run_spec(thread=thread, runnable="chat", primary=(TextPart(text),))


def reply(value: object) -> ModelCallResult:
    return ModelCallResult(
        message=Message.assistant(
            value if isinstance(value, str) else json.dumps(value)
        )
    )


def constrain(harness: ExecutionHarness, *, context: int = 14000) -> None:
    harness.setup = replace(
        harness.setup,
        models=ModelCollection(
            tuple(
                replace(
                    entry,
                    info=replace(
                        entry.info, context_window=context, max_output_tokens=512
                    ),
                )
                for entry in harness.setup.models.entries
            )
        ),
    )


def test_compact_before_model_and_freeze_horizon_for_next_root(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[reply("old " * 18000), reply("middle"), reply("recent")],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            old = await harness.executor.run(spec(harness, thread, "old input"))
            await harness.executor.run(spec(harness, thread, "middle input"))
            recent = await harness.executor.run(spec(harness, thread, "recent input"))
            assert old.status == recent.status == "succeeded"
            constrain(harness)
            summary = {
                "thread": thread,
                "begin": None,
                "end": recent.id,
                "summary": "Earlier request completed.",
            }
            harness.adapter._responses.extend(
                [
                    ModelCallResult(
                        tool_calls=(
                            ToolCall(
                                "history",
                                "history",
                                "history__read_runs",
                                {
                                    "thread": f"compact_{thread}",
                                    "from_end": True,
                                    "limit": 1,
                                },
                            ),
                        )
                    ),
                    reply({"summary": "", "position": None, "complete": False}),
                    reply(
                        {
                            "summary": summary["summary"],
                            "position": None,
                            "complete": True,
                        }
                    ),
                    reply("true"),
                    reply(summary),
                    reply("now"),
                    reply("later"),
                ]
            )
            current = await harness.executor.run(
                spec(harness, thread, "current input"), tracer=tracer
            )
            assert current.status == "succeeded", current.error
            steps = harness.store.list_steps(run_id=current.id)
            assert [step.kind for step in steps] == ["tool", "model"]
            tool, model = steps
            assert (
                isinstance(tool.given, ToolStepGiven)
                and tool.given.trigger == "runtime"
            )
            controls = [
                c
                for c in harness.store.list_run_controls(run_id=current.id)
                if isinstance(c.payload, CompactControlPayload)
            ]
            assert len(controls) == 1
            control = controls[0]
            assert control.triggered_by == tool.ref
            assert control.ref in model.preceded_by
            history = RunHistory(harness.store)
            compact = history.get_compaction(thread)
            assert compact is not None
            roots = history.thread_view(
                f"compact_{thread}", include_children=False
            ).roots
            assert len(roots) == 1 and roots[0].parent is None
            compact_steps = [
                s
                for member in history.thread_view(f"compact_{thread}").members
                for s in harness.store.list_steps(run_id=member.id)
            ]
            assert any(
                isinstance(s.given, ToolStepGiven)
                and s.given.call.name == "history__read_runs"
                and s.given.trigger == "model"
                for s in compact_steps
            )
            assert [
                history.get_model_call(s.ref)
                for s in compact_steps
                if s.kind == "model"
            ] == [i.call for i in harness.adapter.invocations[3:-1]]
            request = history.get_model_call(model.ref)
            assert request == harness.adapter.invocations[-1].call
            assert request.max_output_tokens == 512
            assert not any(t.name.startswith("history__") for t in request.tools)
            text = str([m.to_data() for m in request.messages])
            assert (
                summary["summary"] in text
                and "recent input" in text
                and "current input" in text
            )
            assert "old input" not in text and "_toolang__compact" not in text
            later = await harness.executor.run(spec(harness, thread, "later input"))
            assert later.status == "succeeded"
            start = harness.store.list_run_controls(run_id=later.id)[0]
            assert isinstance(start.payload, RunControlPayload)
            assert start.payload.horizon == compact.ref
            assert [s.kind for s in harness.store.list_steps(run_id=later.id)] == [
                "model"
            ]
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())


async def seed(harness):
    thread = harness.threads.create(prefix=ThreadPrefix.TERM)
    for text in ("old", "middle", "recent"):
        run = await harness.executor.run(spec(harness, thread, text))
        assert run.status == "succeeded"
    constrain(harness)
    return thread, run.id


def compact_responses(thread, end, *, summary="Earlier facts."):
    return [
        reply({"summary": "", "position": None, "complete": False}),
        reply({"summary": summary, "position": None, "complete": True}),
        reply("true"),
        reply({"thread": thread, "begin": None, "end": end, "summary": summary}),
    ]


def seeded_harness(tmp_path):
    return ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[reply("old " * 18000), reply("middle"), reply("recent")],
    )


@pytest.mark.parametrize("failure", ["provider", "wrong_range", "empty", "oversized"])
def test_compact_failure_never_dispatches_the_oversized_normal_call(tmp_path, failure):
    harness = seeded_harness(tmp_path)

    async def scenario():
        async with harness:
            thread, end = await seed(harness)
            turns = compact_responses(
                thread,
                end,
                summary=(
                    ""
                    if failure == "empty"
                    else "large " * 15000
                    if failure == "oversized"
                    else "facts"
                ),
            )
            if failure == "provider":
                turns = [RuntimeError("provider unavailable")]
            elif failure == "wrong_range":
                turns[-1] = reply(
                    {"thread": thread, "begin": end, "end": end, "summary": "partial"}
                )
            harness.adapter._responses.extend(turns)
            current = await harness.executor.run(spec(harness, thread, "current"))
            assert current.status == "failed"
            assert [s.kind for s in harness.store.list_steps(run_id=current.id)] == [
                "tool"
            ]
            assert not [
                c
                for c in harness.store.list_run_controls(run_id=current.id)
                if isinstance(c.payload, CompactControlPayload)
            ]
            assert (
                len(
                    RunHistory(harness.store)
                    .thread_view(f"compact_{thread}", include_children=False)
                    .roots
                )
                == 1
            )

    asyncio.run(scenario())


def test_irreducible_current_input_does_not_start_compact(tmp_path):
    harness = seeded_harness(tmp_path)

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            constrain(harness)
            current = await harness.executor.run(
                spec(harness, thread, "large " * 15000)
            )
            assert current.status == "failed"
            assert not harness.store.list_steps(run_id=current.id)
            assert harness.store.get_thread(thread_id=f"compact_{thread}") is None
            assert not harness.adapter.invocations

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["cancel", "steer", "reload"])
def test_waiting_compact_reprepares_after_controls(tmp_path, action):
    harness = seeded_harness(tmp_path)
    entered = asyncio.Event()
    tracer = RecordingRunTracer()

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            from toolang.execution.events import StepBegin

            await super().on_event(event)
            if isinstance(event, StepBegin) and event.kind == "tool":
                entered.set()

    async def scenario():
        async with harness:
            thread, end = await seed(harness)
            lock = harness.store.db_path.with_name(f"runs.db.{thread}.compact.lock")
            async with permit(lock):
                handle = harness.executor.run(
                    spec(harness, thread, "current"), tracer=hooked
                )
                await asyncio.wait_for(entered.wait(), 2)
                if action == "cancel":
                    control = handle.cancel()
                    current = await asyncio.wait_for(handle, 2)
                    assert current.status == "canceled"
                    assert (
                        harness.store.get_thread(thread_id=f"compact_{thread}") is None
                    )
                    assert (
                        harness.store.list_steps(run_id=current.id)[0].aborted_by
                        == control.ref
                    )
                    return
                if action == "steer":
                    handle.steer(
                        Message.user("additional requirement"), timing="next_call"
                    )
                else:
                    from toolang.state.prepare import prepare_agent_state

                    (harness.setup.layout.home / "agent.too").write_text(
                        SOURCE.replace("  context:", "  recall = none\n  context:"),
                        encoding="utf-8",
                    )
                    state = prepare_agent_state(harness.setup.layout)
                    handle.reload(state=state)
                harness.adapter._responses.extend(
                    [
                        *(compact_responses(thread, end) if action == "steer" else []),
                        reply("done"),
                    ]
                )
            current = await asyncio.wait_for(handle, 3)
            assert current.status == "succeeded", current.error
            text = str(
                [m.to_data() for m in harness.adapter.invocations[-1].call.messages]
            )
            if action == "steer":
                assert "additional requirement" in text
            else:
                assert harness.store.get_thread(thread_id=f"compact_{thread}") is None
        tracer.events.extend(hooked.events)
        assert_replayed(harness.store.db_path, tracer.events)

    hooked = Tracer()
    asyncio.run(scenario())


def test_canceling_compact_owner_cancels_its_independent_run(tmp_path):
    harness = seeded_harness(tmp_path)
    gate = AsyncGate()

    async def scenario():
        async with harness:
            thread, _end = await seed(harness)
            harness.adapter._responses.append(ScriptedModelTurn(reply({}), gate=gate))
            handle = harness.executor.run(spec(harness, thread, "current"))
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            handle.cancel()
            current = await asyncio.wait_for(handle, 2)
            assert current.status == "canceled"
            compact = RunHistory(harness.store).thread_view(f"compact_{thread}")
            assert len(compact.roots) == 1 and compact.roots[0].status == "canceled"
            assert all(run.status == "canceled" for run in compact.members)
            assert RunHistory(harness.store).get_compaction(thread) is None

    asyncio.run(scenario())


def test_parallel_children_share_compact_output_but_adopt_separately(tmp_path):
    source = (
        SOURCE
        + "\nflow parallel(_: Part[]) -> Text[]:\n  storm 2 using chat in 2 lanes\n"
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[reply("old " * 18000), reply("middle"), reply("recent")],
    )
    gate = AsyncGate()

    async def scenario():
        async with harness:
            thread, end = await seed(harness)
            turns = compact_responses(thread, end)
            harness.adapter._responses.extend(
                [
                    ScriptedModelTurn(turns[0], gate=gate),
                    *turns[1:],
                    reply("child one"),
                    reply("child two"),
                ]
            )
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="parallel",
                    primary=Message.user("work").parts,
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            gate.release()
            root = await asyncio.wait_for(handle, 3)
            assert root.status == "succeeded", root.error
            history = RunHistory(harness.store)
            compact = history.thread_view(f"compact_{thread}")
            assert len(compact.roots) == 1
            children = [
                r
                for r in history.thread_view(thread).members
                if r.parent is not None and r.parent.run_id == root.id
            ]
            assert len(children) == 2
            horizons = []
            for child in children:
                controls = [
                    c
                    for c in harness.store.list_run_controls(run_id=child.id)
                    if isinstance(c.payload, CompactControlPayload)
                ]
                assert len(controls) == 1
                assert isinstance(controls[0].payload, CompactControlPayload)
                horizons.append(controls[0].payload.horizon)
            assert horizons[0] == horizons[1]

    asyncio.run(scenario())


def test_oversized_completed_summary_fails_without_repeating_the_range(tmp_path):
    harness = seeded_harness(tmp_path)

    async def scenario():
        async with harness:
            thread, end = await seed(harness)
            turns = compact_responses(thread, end)
            turns[-1] = reply(
                {
                    "thread": thread,
                    "begin": None,
                    "end": end,
                    "summary": "large " * 15000,
                }
            )
            harness.adapter._responses.extend(turns)
            current = await harness.executor.run(spec(harness, thread, "current"))
            assert current.status == "failed"
            assert (
                len(
                    [
                        c
                        for c in harness.store.list_run_controls(run_id=current.id)
                        if isinstance(c.payload, CompactControlPayload)
                    ]
                )
                == 1
            )
            assert [s.kind for s in harness.store.list_steps(run_id=current.id)] == [
                "tool"
            ]
            assert (
                len(RunHistory(harness.store).thread_view(f"compact_{thread}").roots)
                == 1
            )

    asyncio.run(scenario())


def test_cancel_before_oversized_first_call_still_has_a_canceled_step(tmp_path):
    harness = seeded_harness(tmp_path)

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            constrain(harness)
            handle = harness.executor.run(spec(harness, thread, "large " * 15000))
            handle.cancel(timing="next_call")
            root = await handle
            assert root.status == "canceled"
            steps = harness.store.list_steps(run_id=root.id)
            assert [(s.kind, s.status) for s in steps] == [("model", "canceled")]
            assert not harness.adapter.invocations

    asyncio.run(scenario())


def test_committed_compact_control_survives_delivery_failure(tmp_path, monkeypatch):
    harness = seeded_harness(tmp_path)

    async def scenario():
        async with harness:
            thread, end = await seed(harness)
            harness.adapter._responses.extend(compact_responses(thread, end))
            accept = harness.store.accept_compact_control

            def fail_delivery(**kwargs):
                accept(**kwargs)
                raise RuntimeError("delivery interrupted")

            with monkeypatch.context() as patch:
                patch.setattr(harness.store, "accept_compact_control", fail_delivery)
                root = await harness.executor.run(spec(harness, thread, "current"))
            assert root.status == "failed"
            controls = [
                c
                for c in harness.store.list_run_controls(run_id=root.id)
                if isinstance(c.payload, CompactControlPayload)
            ]
            assert len(controls) == 1 and controls[0].status == "applied"
            assert not [
                s for s in harness.store.list_steps(run_id=root.id) if s.kind == "model"
            ]
            harness.adapter._responses.append(reply("next"))
            later = await harness.executor.run(spec(harness, thread, "next input"))
            assert later.status == "succeeded"
            assert [s.kind for s in harness.store.list_steps(run_id=later.id)] == [
                "model"
            ]
            assert (
                len(RunHistory(harness.store).thread_view(f"compact_{thread}").roots)
                == 1
            )

    asyncio.run(scenario())
