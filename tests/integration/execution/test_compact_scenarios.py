"""Automatic compaction stays outside the normal conversation and is replayable."""

from tests.support.setup import replace_materialized_setup

import asyncio
from dataclasses import replace
import json
from pathlib import Path

import pytest

from tests.support.execution_assertions import (
    assert_replayed,
    without_runtime_snapshots,
)
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    RecordingTool,
    ScriptedModelTurn,
)
from toolang.base.types.message import (
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    message_text,
)
from toolang.base.types.model import Reasoning
from toolang.base.model_settings import apply_model_override, parse_model_body
from toolang.base.types.policy import AgentCeiling
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.inspection.history import RunHistory
from toolang.execution.assembly.history import summary_message
from toolang.execution.compaction import permit
from toolang.execution.tokens import InputEstimate
from toolang.execution.records import CompactControlPayload, RunControlPayload
from toolang.execution.types import FieldRef, ThreadPrefix, ToolStepGiven


def compact_runs(harness, thread):
    return [
        r
        for r in harness.store.list_thread_runs_chronological(thread_id=thread)
        if r.parent is not None
        and harness.store.get_run_control(run_id=r.id, index=0).payload.runnable
        == "_:compact"
    ]


SOURCE = """agic chat(_: Part[]) -> Text:
  context = none
  instruct = none
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
    normal = replace(
        harness.setup.models_effective()[0],
        limit={"context": context, "output": 512},
        structured_output=True,
    )
    # Complete historical roots must fit the reducer, even when they no longer
    # fit the smaller caller's assembled context.
    reducer = replace(
        normal, id="reducer", name="reducer", limit={"context": 200000, "output": 8192}
    )
    harness.setup = replace_materialized_setup(
        harness.setup,
        models=(normal, reducer),
        compact_model=parse_model_body("test/reducer"),
    )


@pytest.mark.parametrize("next_root", ["run", "rerun"])
@pytest.mark.parametrize("output_limit", [None, 512])
def test_compact_before_model_and_freeze_horizon_for_next_root(
    tmp_path: Path, next_root: str, output_limit: int | None
) -> None:
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
            if output_limit is None:
                harness.setup = replace_materialized_setup(
                    harness.setup,
                    models=tuple(
                        replace(model, limit={"context": 14000})
                        if model.id == "scripted"
                        else model
                        for model in harness.setup.models_effective()
                    ),
                )
            summary = {
                "thread": thread,
                "begin": None,
                "end": recent.id,
                "summary": "Earlier request completed.",
            }
            harness.adapter._responses.extend(
                [
                    ModelCallResult(message=Message.assistant(summary["summary"])),
                    reply("now"),
                    reply("later"),
                ]
            )
            current = await harness.executor.run(
                spec(harness, thread, "current input"), tracer=tracer
            )
            assert current.status == "succeeded", (
                harness.store.resolve_error(current.error) if current.error else None
            )
            steps = harness.store.list_steps(run_id=current.id)
            assert [step.kind for step in steps] == ["tool", "model"]
            tool, model = steps
            start = harness.store.list_run_controls(run_id=current.id)[0]
            assert model.input == (
                FieldRef.from_path(start.ref, "payload", "input", "_"),
            )
            assert (
                isinstance(tool.given, ToolStepGiven)
                and tool.given.trigger == "runtime"
            )
            assert tool.given.call.input == {}
            controls = [
                c
                for c in harness.store.list_run_controls(run_id=current.id)
                if isinstance(c.payload, CompactControlPayload)
            ]
            assert len(controls) == 1
            control = controls[0]
            assert isinstance(control.payload, CompactControlPayload)
            assert tool.output is not None and isinstance(
                tool.output.local.value, ToolResultPart
            )
            assert tool.output.local.value.output == {
                "controls": [
                    {"ref": str(control.ref), "horizon": str(control.payload.horizon)}
                ]
            }
            assert control.triggered_by == tool.ref
            assert control.ref in model.preceded_by
            history = RunHistory(harness.store)
            compact = history.get_compaction(thread)
            assert compact is not None
            roots = compact_runs(harness, thread)
            assert len(roots) == 1 and all(
                root.parent == tool.ref and str(root.thread) == thread for root in roots
            )
            compact_steps = [
                s
                for member in compact_runs(harness, thread)
                for s in harness.store.list_steps(run_id=member.id)
            ]
            assert any(
                isinstance(s.given, ToolStepGiven)
                and s.given.call.name == "_toolang__compact_read"
                and s.given.trigger == "runtime"
                for s in compact_steps
            )
            assert [
                history.get_model_call(s.ref)
                for s in compact_steps
                if s.kind == "model"
            ] == [i.call for i in harness.adapter.invocations[3:-1]]
            request = history.get_model_call(model.ref)
            assert request == harness.adapter.invocations[-1].call
            expected_output = 3500 if output_limit is None else output_limit
            assert request.max_output_tokens == expected_output
            assert all(
                i.call.max_output_tokens == 8192 and not i.call.tools
                for i in harness.adapter.invocations[3:-1]
            )
            assert not any(t.name.startswith("history__") for t in request.tools)
            text = str([m.to_data() for m in request.messages])
            assert (
                summary["summary"] in text
                and "recent input" in text
                and "current input" in text
            )
            assert "old input" not in text and "_toolang__compact" not in text
            later = await (
                harness.executor.run(spec(harness, thread, "later input"))
                if next_root == "run"
                else harness.executor.rerun(
                    old.id, setup=harness.setup, state=harness.state
                )
            )
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
        ModelCallResult(message=Message.assistant(summary)),
    ]


def seeded_harness(tmp_path):
    return ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[reply("old " * 18000), reply("middle"), reply("recent")],
    )


def test_compact_between_model_calls_preserves_now_and_prior_call(tmp_path):
    tool = RecordingTool("lookup__read", output={"text": "data " * 3000})
    call = ToolCall("lookup", "lookup", tool.name, {})
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[reply("old " * 6000), reply("middle"), reply("recent")],
        tools={tool.name: tool},
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread, end = await seed(harness)
            # Leave room for current input, but not the large tool result,
            # independently of the bundled protocol's length.
            baseline = InputEstimate().count(harness.adapter.invocations[-1].call, None)
            constrain(harness, context=baseline + 4096)
            harness.adapter._responses.extend(
                [
                    ModelCallResult(
                        tool_calls=(call,),
                        continuation={"previous_response_id": "old-context"},
                    ),
                    *compact_responses(thread, end),
                    reply("done"),
                ]
            )
            root = await harness.executor.run(
                spec(harness, thread, "current input"), tracer=tracer
            )
            assert root.status == "succeeded", root.error
            steps = harness.store.list_steps(run_id=root.id)
            assert [step.kind for step in steps] == ["model", "tool", "tool", "model"]
            first, lookup, compact, last = steps
            assert last.input == (
                FieldRef.from_path(lookup.ref, "output", "local", "value"),
            )
            assert isinstance(lookup.given, ToolStepGiven)
            assert lookup.given.trigger == "model"
            assert isinstance(compact.given, ToolStepGiven)
            assert compact.given.trigger == "runtime"
            controls = [
                c
                for c in harness.store.list_run_controls(run_id=root.id)
                if isinstance(c.payload, CompactControlPayload)
            ]
            assert len(controls) == 1
            assert controls[0].triggered_by == compact.ref
            assert controls[0].ref in last.preceded_by
            history = RunHistory(harness.store)
            before = history.get_model_call(first.ref)
            after = history.get_model_call(last.ref)
            assert before == harness.adapter.invocations[3].call
            assert after == harness.adapter.invocations[-1].call
            assert after.continuation == {"previous_response_id": "old-context"}
            assert after.max_output_tokens == before.max_output_tokens == 512
            assert "old old" in str([m.to_data() for m in before.messages])
            text = str([m.to_data() for m in after.messages])
            assert "Earlier facts." in text and "old old" not in text
            assert "recent" in text and "current input" in text
            assert "_toolang__compact" not in text
            exchange = [
                part
                for message in after.messages
                for part in message.parts
                if isinstance(part, (ToolCallPart, ToolResultPart))
            ]
            assert [type(part) for part in exchange] == [ToolCallPart, ToolResultPart]
            assert all(part.call_id == call.call_id for part in exchange)
            assert isinstance(exchange[1], ToolResultPart)
            assert exchange[1].output == tool.output
            assert len(tool.calls) == 1
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())


def test_unrecorded_flow_tails_remain_compactable(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE + "\nflow job(_: Part[]) -> Text:\n  run chat\n",
        responses=[reply("old " * 18000), reply("middle"), reply("recent")],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            roots = []
            for text in ("old", "middle", "recent"):
                root = await harness.executor.run(
                    harness.run_spec(
                        thread=thread, runnable="flow:job", primary=(TextPart(text),)
                    )
                )
                assert root.status == "succeeded", root.error
                roots.append(root)
            constrain(harness)
            harness.adapter._responses.extend(
                [*compact_responses(thread, roots[1].id), reply("done")]
            )
            root = await harness.executor.run(
                spec(harness, thread, "current"), tracer=tracer
            )
            assert root.status == "succeeded", root.error
            steps = harness.store.list_steps(run_id=root.id)
            assert [step.kind for step in steps] == ["tool", "model"]
            request = RunHistory(harness.store).get_model_call(steps[-1].ref)
            assert request == harness.adapter.invocations[-1].call
            assert without_runtime_snapshots(request.messages) == [
                summary_message("Earlier facts."),
                Message.user("middle"),
                Message.assistant("middle"),
                Message.user("recent"),
                Message.assistant("recent"),
                Message.user("current"),
            ]
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["provider", "empty"])
def test_compact_failure_never_dispatches_the_oversized_normal_call(tmp_path, failure):
    harness = seeded_harness(tmp_path)

    async def scenario():
        async with harness:
            thread, end = await seed(harness)
            turns = compact_responses(
                thread,
                end,
                summary="" if failure == "empty" else "facts",
            )
            if failure == "provider":
                turns = [RuntimeError("provider unavailable")]
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
            assert len(compact_runs(harness, thread)) == 1

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


@pytest.mark.parametrize("oversized", ["now", "required_near"])
def test_large_current_input_fails_but_oversized_retained_step_is_bounded(
    tmp_path, oversized
):
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            reply("old output"),
            reply("middle output"),
            reply(
                "large " * 15000 if oversized == "required_near" else "recent output"
            ),
        ],
    )

    async def scenario():
        async with harness:
            thread, _end = await seed(harness)
            harness.adapter._responses.append(reply("continued"))
            root = await harness.executor.run(
                spec(
                    harness,
                    thread,
                    "large " * 15000 if oversized == "now" else "current",
                )
            )
            if oversized == "required_near":
                assert root.status == "succeeded", root.error
                assert "omitted oversized Step content" in str(
                    harness.adapter.invocations[-1].call.messages
                )
                return
            assert root.status == "failed"
            assert harness.store.get_thread(thread_id=f"compact_{thread}") is None
            assert not harness.store.list_steps(run_id=root.id)
            assert len(harness.adapter.invocations) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("selection", ["auto", "explicit", "ceiling"])
def test_compact_selects_its_own_model_and_parameters(tmp_path, selection):
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE.replace("  context =", "  models = test/scripted\n  context ="),
        responses=[reply("old " * 18000), reply("middle"), reply("recent")],
    )

    async def scenario():
        async with harness:
            thread, end = await seed(harness)
            original = harness.setup.models_effective()[0]
            reasoning_options = ({"type": "effort", "values": ["low", "high"]},)
            normal = replace(
                original,
                tool_call=True,
                limit={"context": 80000, "output": 8192},
                structured_output=False,
                reasoning_options=reasoning_options,
            )
            candidates = tuple(
                replace(
                    original,
                    id=name,
                    name=name,
                    limit={"context": 200000, "output": 8192},
                    reasoning_options=reasoning_options,
                )
                for name in ("first", "second")
            )
            configured = (
                parse_model_body("test/second effort=low")
                if selection in {"explicit", "ceiling"}
                else None
            )
            harness.setup = replace_materialized_setup(
                harness.setup,
                models=(normal, *candidates),
                compact_model=configured,
            )
            harness.setup = replace(
                harness.setup,
                compact=replace(harness.setup.compact, trigger=12000, recent=4200),
            )
            harness.adapter._responses.extend(
                [*compact_responses(thread, end), reply("done")]
            )
            request = replace(
                spec(harness, thread, "current"),
                model_request=apply_model_override(
                    None,
                    None,
                    parse_model_body("test/scripted effort=high max_output=512"),
                ),
                ceilings=(AgentCeiling(models=("test/second", "test/scripted")),)
                if selection == "ceiling"
                else (),
            )
            current = await harness.executor.run(request)
            assert current.status == "succeeded", (
                harness.store.resolve_error(current.error) if current.error else None
            )
            expected = "test/scripted" if selection == "auto" else "test/second"
            compact_calls = harness.adapter.invocations[3:-1]
            assert len(compact_calls) == 1
            assert all(call.model.ref == expected for call in compact_calls)
            assert compact_calls[0].call.max_output_tokens == 8192
            assert harness.adapter.invocations[-1].call.max_output_tokens == 512
            assert all(
                call.call.reasoning
                == (Reasoning("low") if selection in {"explicit", "ceiling"} else None)
                for call in compact_calls
            )
            assert harness.adapter.invocations[-1].call.reasoning == Reasoning("high")
            history = RunHistory(harness.store)
            for root in compact_runs(harness, thread):
                for step in harness.store.list_steps(run_id=root.id):
                    if step.kind == "model":
                        # The durable call record does not persist the
                        # effective reasoning yet (deferred records change), so
                        # compare the rebuilt call with that control normalized.
                        assert replace(
                            history.get_model_call(step.ref), reasoning=None
                        ) in [
                            replace(call.call, reasoning=None) for call in compact_calls
                        ]
            root = compact_runs(harness, thread)[0]
            payload = harness.store.list_run_controls(run_id=root.id)[0].payload
            assert isinstance(payload, RunControlPayload)
            assert (
                payload.model_request is not None
                and payload.model_request.ref == expected
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("setting", ["unset", "test/outside"])
def test_compact_invalid_or_unauthorized_does_not_start_a_run(tmp_path, setting):
    harness = seeded_harness(tmp_path)

    async def scenario():
        async with harness:
            thread, _end = await seed(harness)
            harness.setup = replace(
                harness.setup,
                compact=replace(harness.setup.compact, model=parse_model_body(setting)),
            )
            current = await harness.executor.run(spec(harness, thread, "current"))
            assert current.status == "failed"
            assert harness.store.get_thread(thread_id=f"compact_{thread}") is None
            assert len(harness.adapter.invocations) == 3

    asyncio.run(scenario())


def test_compact_requires_a_model_that_can_read_history(tmp_path):
    harness = seeded_harness(tmp_path)

    async def scenario():
        async with harness:
            thread, end = await seed(harness)
            harness.setup = replace_materialized_setup(
                harness.setup,
                models=tuple(
                    replace(model, tool_call=False)
                    for model in harness.setup.models_effective()
                ),
            )
            harness.adapter._responses.extend(
                [*compact_responses(thread, end), reply("done")]
            )
            current = await harness.executor.run(spec(harness, thread, "current"))
            assert current.status == "failed"
            steps = harness.store.list_steps(run_id=current.id)
            assert len(steps) == 1 and "tool calls" in str(steps[0].error)
            assert harness.store.get_thread(thread_id=f"compact_{thread}") is None
            assert len(harness.adapter.invocations) == 3

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
                        SOURCE.replace("  context =", "  recall = none\n  context ="),
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
            assert current.status == "succeeded", (
                harness.store.resolve_error(current.error) if current.error else None
            )
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


@pytest.mark.parametrize("action", ["cancel", "steer"])
def test_interrupting_compact_owner_cancels_its_child_run(tmp_path, action):
    harness = seeded_harness(tmp_path)
    gate = AsyncGate()
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread, end = await seed(harness)
            harness.adapter._responses.append(ScriptedModelTurn(reply({}), gate=gate))
            handle = harness.executor.run(
                spec(harness, thread, "current"), tracer=tracer
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            if action == "cancel":
                handle.cancel()
            else:
                harness.adapter._responses.extend(
                    [*compact_responses(thread, end), reply("done")]
                )
                steer = handle.steer(
                    Message.user("adjusted request"), timing="immediate"
                )
            current = await asyncio.wait_for(handle, 2)
            compact = compact_runs(harness, thread)
            assert compact[0].status == "canceled"
            if action == "cancel":
                assert current.status == "canceled"
                assert len(compact) == 1
                assert all(run.status == "canceled" for run in compact)
                assert RunHistory(harness.store).get_compaction(thread) is None
            else:
                assert current.status == "succeeded", (
                    harness.store.resolve_error(current.error)
                    if current.error
                    else None
                )
                assert len(compact) == 2
                first, retried, model = harness.store.list_steps(run_id=current.id)
                assert first.status == "canceled" and first.aborted_by == steer.ref
                assert retried.status == "succeeded" and retried.kind == "tool"
                start = harness.store.list_run_controls(run_id=current.id)[0]
                assert model.input == (
                    FieldRef.from_path(start.ref, "payload", "input", "_"),
                    FieldRef.from_path(steer.ref, "payload", "input", "_"),
                )
                assert "adjusted request" in str(
                    [m.to_data() for m in harness.adapter.invocations[-1].call.messages]
                )
        assert_replayed(harness.store.db_path, tracer.events)

    asyncio.run(scenario())


def test_parallel_children_do_not_automatically_recall_or_compact_root_history(
    tmp_path,
):
    source = (
        SOURCE
        + "\nflow parallel(_: Part[]) -> Text[]:\n  storm 2 using chat in 2 lanes\n"
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[
            reply("old " * 18000),
            reply("middle"),
            reply("recent"),
            reply("child one"),
            reply("child two"),
        ],
    )

    async def scenario():
        async with harness:
            thread, _ = await seed(harness)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="parallel",
                    primary=Message.user("work").parts,
                )
            )
            assert root.status == "succeeded", root.error
            assert len(harness.adapter.invocations) == 5
            for invocation in harness.adapter.invocations[3:]:
                assert all(
                    "old old" not in message_text(message.parts)
                    for message in invocation.call.messages
                )
            for child in harness.store.list_run_tree(root_run_id=root.id):
                assert not any(
                    isinstance(control.payload, CompactControlPayload)
                    for control in harness.store.list_run_controls(run_id=child.id)
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


def test_automatic_publication_survives_interrupted_control_adoption(
    tmp_path, monkeypatch
):
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
            assert controls == []
            assert harness.store.get_thread(thread_id=thread).horizon is None
            history = RunHistory(harness.store)
            assert history.get_compaction(thread) is None
            producers = compact_runs(harness, thread)
            assert len(producers) == 1 and producers[0].status == "succeeded"
            assert not [
                s for s in harness.store.list_steps(run_id=root.id) if s.kind == "model"
            ]
            # The published producer survives; a fresh caller adopts it without reduction.
            harness.adapter._responses.extend([reply("next")])
            later = await harness.executor.run(spec(harness, thread, "next input"))
            assert later.status == "succeeded", later.error
            assert history.get_compaction(thread) is not None
            assert len(compact_runs(harness, thread)) == 1

    asyncio.run(scenario())


def test_automatic_incremental_compaction_freezes_previous_coverage(tmp_path):
    harness = seeded_harness(tmp_path)

    async def scenario():
        async with harness:
            thread, end = await seed(harness)
            harness.adapter._responses.extend(
                [
                    *compact_responses(thread, end),
                    reply("large output " * 12000),
                ]
            )
            first = await harness.executor.run(spec(harness, thread, "first compact"))
            assert first.status == "succeeded", first.error
            history = RunHistory(harness.store)
            previous = history.get_compaction(thread)
            assert previous is not None
            # Add two small roots after the large terminal reply.
            constrain(harness, context=1_000_000)
            harness.adapter._responses.extend(
                [reply("small"), reply("small retained output")]
            )
            intermediate = await harness.executor.run(
                spec(harness, thread, "record output")
            )
            assert intermediate.status == "succeeded", intermediate.error
            retained = await harness.executor.run(spec(harness, thread, "retained"))
            assert retained.status == "succeeded", retained.error
            constrain(harness)
            harness.adapter._responses.extend(
                [
                    *compact_responses(thread, retained.id, summary="Combined prefix."),
                    reply("done"),
                ]
            )
            final = await harness.executor.run(spec(harness, thread, "continue"))
            assert final.status == "succeeded", final.error
            latest = history.get_compaction(thread)
            assert latest is not None and latest.ref != previous.ref
            assert latest.result.begin == previous.result.begin
            assert str(latest.result.end) == intermediate.id
            control = harness.store.get_run_control(
                run_id=compact_runs(harness, thread)[-1].id, index=0
            )
            assert isinstance(control.payload, RunControlPayload)
            assert control.payload.input["summary"] == previous.result.summary
            assert control.payload.input["begin"] == str(previous.result.end)
            text = str(harness.adapter.invocations[-1].call.messages)
            assert "Combined prefix." in text and "large output" not in text

    asyncio.run(scenario())


def test_child_fixed_input_does_not_compact_unused_root_history(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE + "\nflow parent:\n  run chat\n",
        responses=[reply("old " * 18000), reply("middle"), reply("recent")],
    )

    async def scenario():
        async with harness:
            thread, end = await seed(harness)
            harness.adapter._responses.extend(compact_responses(thread, end))
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="parent",
                    primary=(TextPart("fixed " * 30000),),
                )
            )
            assert root.status == "failed"
            assert harness.store.get_thread(thread_id=f"compact_{thread}") is None
            assert len(harness.adapter.invocations) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("layer", ["user", "context", "instruct"])
@pytest.mark.parametrize("root", ["reader", "parent"])
def test_compact_budget_rerenders_explicit_history_in_all_prompt_layers(
    tmp_path, layer, root
):
    source = SOURCE
    if layer != "user":
        source += f"\n{layer} history: History: {{{{_past}}}}\n"
    source += "\nagic reader() -> Text:\n"
    for setting in ("context", "instruct"):
        source += f"  {setting} = {'history' if setting == layer else 'none'}\n"
    if layer == "user":
        source += "  user: History: {{_past}}\n"
    source += "flow parent() -> Text:\n  run reader\n"
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[reply("old " * 18000), reply("middle"), reply("recent")],
    )

    async def scenario():
        async with harness:
            thread, end = await seed(harness)
            harness.adapter._responses.extend(
                [*compact_responses(thread, end), reply("done")]
            )
            run = await harness.executor.run(
                harness.run_spec(thread=thread, runnable=root)
            )
            assert run.status == "succeeded", (
                harness.store.resolve_error(run.error) if run.error else None
            )
            assert len(harness.adapter.invocations) == 5
            final = harness.adapter.invocations[-1].call
            rendered = final.instructions + "\n".join(
                message_text(message.parts) for message in final.messages
            )
            assert "old old" not in rendered
            assert "Earlier facts." in rendered
            assert "recent" in rendered

    asyncio.run(scenario())


def test_runtime_compact_resolves_its_boundary_at_admission(tmp_path, monkeypatch):
    from toolang.execution.executor.steps import model as model_step
    from toolang.execution.types import RunRef

    harness = seeded_harness(tmp_path)

    async def scenario():
        async with harness:
            thread, retained = await seed(harness)
            # The initial preflight retains the two small recent roots. Admission
            # can choose a newer boundary without an obsolete argument rejecting it.
            monkeypatch.setattr(
                model_step, "compaction_boundary", lambda state: RunRef(retained)
            )
            harness.adapter._responses.extend([reply("summary"), reply("done")])
            current = await harness.executor.run(spec(harness, thread, "current"))
            assert current.status == "succeeded", current.error
            output = RunHistory(harness.store).get_compaction(thread)
            assert output is not None and output.result.end == retained
            steps = harness.store.list_steps(run_id=current.id)
            assert isinstance(steps[0].given, ToolStepGiven)
            assert steps[0].given.call.input == {}
            assert [step.kind for step in steps] == ["tool", "model"]

    asyncio.run(scenario())


@pytest.mark.parametrize("recent,keep_middle", [("1%", False), ("50%", True)])
@pytest.mark.parametrize("summary,target", [(400, 400), ("2%", 200)])
def test_compact_config_controls_summary_and_recent_whole_roots(
    tmp_path, recent, keep_middle, summary, target
):
    from toolang.setup.config import resolve_compact_config

    latest_text = "latest fact " * 100
    middle_text = "middle fact " * 100
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[reply("old " * 18000), reply(middle_text), reply(latest_text)],
    )

    async def scenario():
        async with harness:
            thread, latest = await seed(harness)
            roots = harness.store.list_thread_runs_chronological(thread_id=thread)
            constrain(harness, context=10000)
            harness.setup = replace(
                harness.setup,
                compact=resolve_compact_config(
                    (
                        {
                            "compact": {
                                "model": "test/reducer",
                                "summary": summary,
                                "recent": recent,
                                "trigger": "80%",
                            }
                        },
                    )
                ),
            )
            harness.adapter._responses.extend([reply("Earlier facts."), reply("done")])
            current = await harness.executor.run(spec(harness, thread, "current"))
            assert current.status == "succeeded", (
                harness.store.resolve_error(current.error) if current.error else None
            )
            (child,) = compact_runs(harness, thread)
            control = harness.store.get_run_control(run_id=child.id, index=0)
            assert control is not None and isinstance(
                control.payload, RunControlPayload
            )
            entry = control.payload
            assert entry.input["end"] == (roots[-2].id if keep_middle else latest)
            assert json.loads(str(entry.input["policy"]))["size"] == target
            compact_call = harness.adapter.invocations[-2].call
            assert f"approximately {target} tokens" in compact_call.instructions
            assert compact_call.max_output_tokens == max(target * 2, target + 1024)
            final = str(harness.adapter.invocations[-1].call.messages)
            assert latest_text in final
            assert (middle_text in final) == keep_middle
            assert "Earlier facts." in final

    asyncio.run(scenario())


@pytest.mark.parametrize("recent,success", [(500, True), ("30%", False)])
def test_unknown_thread_context_never_substitutes_an_input_limit(
    tmp_path, recent, success
):
    from toolang.setup.config import resolve_compact_config

    harness = seeded_harness(tmp_path)

    async def scenario():
        async with harness:
            thread, _end = await seed(harness)
            models = tuple(
                replace(m, limit={"input": 14000, "output": 512})
                if m.id == "scripted"
                else m
                for m in harness.setup.models_effective()
            )
            harness.setup = replace_materialized_setup(harness.setup, models=models)
            harness.setup = replace(
                harness.setup,
                compact=resolve_compact_config(
                    (
                        {
                            "compact": {
                                "model": "test/reducer",
                                "summary": 400,
                                "recent": recent,
                                "trigger": 8000,
                            }
                        },
                    )
                ),
            )
            harness.adapter._responses.extend([reply("Earlier facts."), reply("done")])
            current = await harness.executor.run(spec(harness, thread, "current"))
            if success:
                assert current.status == "succeeded", harness.store.resolve_error(
                    current.error
                )
                assert len(compact_runs(harness, thread)) == 1
            else:
                assert current.status == "failed"
                assert (
                    "percentages require thread model limit.context"
                    in harness.store.resolve_error(current.error)
                )
                assert len(harness.adapter.invocations) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("step_horizon", [False, True])
def test_one_large_root_compacts_in_step_batches_and_keeps_latest_step(
    tmp_path, step_horizon, monkeypatch
):
    from toolang.execution import compaction
    from toolang.execution.types import RunRef, StepRef
    from toolang.setup.config import resolve_compact_config

    tool = RecordingTool("lookup__read", output={"text": "large tool body " * 5000})
    calls = [ToolCall(f"lookup-{i}", f"lookup-{i}", tool.name, {}) for i in range(3)]
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[
            *[ModelCallResult(tool_calls=(call,)) for call in calls],
            reply("latest complete step"),
        ],
    )

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            original = await harness.executor.run(
                spec(harness, thread, "original goal")
            )
            assert original.status == "succeeded", original.error
            units = compaction.history_units(harness.store, original)
            assert len(units) == 7
            assert len({u.ref for u in units}) == 7
            for unit in units:
                tool_results = [
                    p
                    for m in unit.messages
                    for p in m.parts
                    if isinstance(p, ToolResultPart)
                ]
                if tool_results:
                    tool_calls = [
                        p
                        for m in unit.messages
                        for p in m.parts
                        if isinstance(p, ToolCallPart)
                    ]
                    assert [p.tool_call_id for p in tool_calls] == [
                        p.tool_call_id for p in tool_results
                    ]
            retained = units[-1].ref
            assert isinstance(retained, StepRef)
            constrain(harness, context=14000)
            models = harness.setup.models_effective()
            harness.setup = replace_materialized_setup(
                harness.setup,
                models=tuple(
                    replace(m, limit={"context": 8000, "output": 1024})
                    if m.id == "reducer"
                    else m
                    for m in models
                ),
            )
            harness.setup = replace(
                harness.setup,
                compact=resolve_compact_config(
                    (
                        {
                            "compact": {
                                "model": "test/reducer",
                                "summary": 128,
                                "recent": 1,
                            }
                        },
                    )
                ),
            )
            harness.adapter._responses.extend([reply("Small cumulative summary.")] * 16)
            step_reads = []
            list_steps_before = harness.store.list_steps

            def observed_steps(*, run_id):
                step_reads.append(run_id)
                return list_steps_before(run_id=run_id)

            monkeypatch.setattr(harness.store, "list_steps", observed_steps)
            caller = await harness.executor.run(spec(harness, thread, "continue"))
            assert caller.status == "succeeded", caller.error
            (producer,) = compact_runs(harness, thread)
            # Checkpoint/adoption validation may read the producer a fixed
            # number of times; allocating each batch must not reread all steps.
            assert step_reads.count(producer.id) <= 7
            history = RunHistory(harness.store)
            output = history.get_compaction(thread)
            assert output is not None and output.result.end == str(retained)
            steps = harness.store.list_steps(run_id=producer.id)
            reads = [s for s in steps if s.kind == "tool"]
            assert len(reads) > 1
            read_inputs = []
            for step in reads:
                assert isinstance(step.given, ToolStepGiven)
                read_inputs.append(step.given.call.input)
            covered = [ref for value in read_inputs for ref in value["units"]]
            assert covered == [str(u.ref) for u in units[:-1]]
            assert all(
                value["roots"] == [original.id] * len(value["units"])
                for value in read_inputs
            )
            model_steps = [s for s in steps if s.kind == "model"]
            requests = [history.get_model_call(s.ref) for s in model_steps]
            assert any(
                "omitted oversized Step content" in (request.messages[1].content or "")
                for request in requests
            )
            for request in requests:
                assert len(request.messages) == 2
                assert request.messages[0].content is not None
                assert request.messages[1].content is not None
                assert request.messages[0].content.startswith("<previous_summary>")
                body = (
                    request.messages[1]
                    .content.removeprefix("<following_messages>")
                    .removesuffix("</following_messages>")
                )
                data = json.loads(body)
                assert all(u["run_id"] == original.id and u["step_id"] for u in data)
            selected = harness.store.message_history(caller.id).select(output.ref)
            assert without_runtime_snapshots(selected.near) == [
                Message.assistant("latest complete step")
            ]
            assert selected.units[0][0] == retained
            assert "large tool body" not in str(
                harness.adapter.invocations[-1].call.messages
            )
            assert "latest complete step" in str(
                harness.adapter.invocations[-1].call.messages
            )
            assert (
                compaction.read_checkpoint(harness.store, producer)[0] == len(units) - 1
            )
            # Recover after the first accepted pair, while still inside this root.
            list_steps = harness.store.list_steps
            with monkeypatch.context() as patch:
                patch.setattr(
                    harness.store,
                    "list_steps",
                    lambda *, run_id: (
                        steps[:2]
                        if run_id == producer.id
                        else list_steps(run_id=run_id)
                    ),
                )
                cursor, summary = compaction.read_checkpoint(harness.store, producer)
            assert 0 < cursor < len(units) - 1
            remaining = {u.ref: u for u in units[cursor:-1]}
            reducer_model = next(
                m for m in harness.setup.models_effective() if m.id == "reducer"
            )
            resumed = compaction.Compaction(
                tuple(remaining),
                remaining.__getitem__,
                reducer_model,
                size=128,
                summary=summary,
            )
            resume_call = resumed.next_call()
            assert resume_call is not None
            assert resumed.batch[0].ref == units[cursor].ref
            assert (
                resume_call.messages[0].content
                == f"<previous_summary>{summary}</previous_summary>"
            )
            assert [str(u.ref) for u in resumed.batch] == covered[
                cursor : cursor + len(resumed.batch)
            ]
            original_output = harness.store.list_steps(run_id=original.id)[1].output
            assert original_output is not None
            assert "large tool body " * 5000 in str(
                harness.store.resolve_output(original_output)
            )
            if step_horizon:
                final = model_steps[-1].ref
                harness.store.publish_compaction(final, roots=(RunRef(original.id),))
                with pytest.raises(ValueError, match="referenced horizon"):
                    with harness.store.write_transaction():
                        harness.store._delete_retry_suffix(
                            tree_runs=(producer.id,), steps=(final,)
                        )
                assert harness.store.get_step(ref=final) is not None
                thread_record = harness.store.get_thread(thread_id=thread)
                assert thread_record is not None and thread_record.horizon == final
                next_run = await harness.executor.run(spec(harness, thread, "next"))
                assert next_run.status == "succeeded", next_run.error
                entry = harness.store.get_run_control(run_id=next_run.id, index=0)
                assert entry is not None and isinstance(
                    entry.payload, RunControlPayload
                )
                assert entry.payload.horizon == final
                restored = RunHistory(harness.store).get_compaction(thread)
                assert restored is not None and restored.ref == final
                assert restored.result == output.result

    asyncio.run(scenario())


def test_recall_none_does_not_bound_unused_history(tmp_path, monkeypatch):
    from toolang.execution.executor import frame

    source = (
        SOURCE
        + "\nagic isolated(_: Part[]) -> Text:\n  recall = none\n  context = none\n  instruct = none\n  user: {{_}}\n"
    )
    h = ExecutionHarness.create(
        tmp_path, source=source, responses=[reply("history"), reply("done")]
    )

    async def scenario():
        async with h:
            thread = h.threads.create(prefix=ThreadPrefix.TERM)
            await h.executor.run(spec(h, thread, "first"))
            constrain(h)

            def unused(*args):
                raise AssertionError("unused history must not be bounded")

            monkeypatch.setattr(frame, "bound_latest_step", unused)
            run = await h.executor.run(
                h.run_spec(
                    thread=thread, runnable="isolated", primary=(TextPart("next"),)
                )
            )
            assert run.status == "succeeded", run.error

    asyncio.run(scenario())


def test_history_units_keep_step_outcome_when_root_fails(tmp_path):
    from toolang.execution.compaction import history_units

    tool = RecordingTool("lookup__read", output={"result": "completed"})
    call = ToolCall("lookup", "lookup", tool.name, {})
    h = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[ModelCallResult(tool_calls=(call,)), RuntimeError("later failure")],
    )

    async def scenario():
        async with h:
            thread = h.threads.create(prefix=ThreadPrefix.TERM)
            run = await h.executor.run(spec(h, thread, "work"))
            assert run.status == "failed"
            tool_step = next(
                s for s in h.store.list_steps(run_id=run.id) if s.kind == "tool"
            )
            unit = next(
                u for u in history_units(h.store, run) if u.step_id == tool_step.ref
            )
            assert unit.status == "succeeded" and unit.run_status == "failed"
            assert unit.created_at == tool_step.started_at

    asyncio.run(scenario())


@pytest.mark.parametrize("repaired", [False, True])
def test_summary_must_fit_before_publication(tmp_path, repaired):
    h = seeded_harness(tmp_path)
    tracer = RecordingRunTracer()

    async def scenario():
        async with h:
            thread, _ = await seed(h)
            h.adapter._responses.extend(
                [reply("summary " * 5000), reply("Short facts."), reply("done")]
                if repaired
                else [reply("summary " * 5000)] * 3
            )
            run = await h.executor.run(spec(h, thread, "continue"), tracer=tracer)
            assert run.status == ("succeeded" if repaired else "failed"), run.error
            horizon = h.store.get_thread(thread_id=thread).horizon
            assert (horizon is not None) == repaired
            controls = [
                c
                for c in h.store.list_run_controls(run_id=run.id)
                if isinstance(c.payload, CompactControlPayload)
            ]
            assert len(controls) == int(repaired)
            (child,) = compact_runs(h, thread)
            steps = h.store.list_steps(run_id=child.id)
            reads = [s.given.call.input["units"] for s in steps if s.kind == "tool"]
            assert reads and all(refs == reads[0] for refs in reads)
            assert len(reads) == (2 if repaired else 3)

    asyncio.run(scenario())
    assert_replayed(h.store.db_path, tracer.events)


def test_provider_context_rejection_compacts_before_retry(tmp_path):
    from toolang.base.errors import ModelResponseError

    h = ExecutionHarness.create(
        tmp_path, source=SOURCE, responses=[reply("old " * 3000), reply("recent")]
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with h:
            thread = h.threads.create(prefix=ThreadPrefix.TERM)
            await h.executor.run(spec(h, thread, "first"))
            await h.executor.run(spec(h, thread, "second"))
            constrain(h, context=30000)
            h.adapter._responses.extend(
                [
                    ModelResponseError(
                        "context_length_exceeded", kind="provider_rejection"
                    ),
                    reply("Compact facts."),
                    reply("done"),
                ]
            )
            run = await h.executor.run(spec(h, thread, "continue"), tracer=tracer)
            assert run.status == "succeeded", (
                h.store.resolve_error(run.error) if run.error else None
            )
            assert len(compact_runs(h, thread)) == 1
            steps = h.store.list_steps(run_id=run.id)
            assert [(s.kind, s.status) for s in steps] == [
                ("model", "failed"),
                ("tool", "succeeded"),
                ("model", "succeeded"),
            ]

    asyncio.run(scenario())


def test_child_without_history_references_does_not_bound_history(tmp_path, monkeypatch):
    from toolang.execution.executor import frame

    source = (
        SOURCE
        + "\nagic child() -> Text:\n  context = none\n  instruct = none\n  user: next\nflow parent() -> Text:\n  run child\n"
    )
    h = ExecutionHarness.create(
        tmp_path, source=source, responses=[reply("history"), reply("done")]
    )

    async def scenario():
        async with h:
            thread = h.threads.create(prefix=ThreadPrefix.TERM)
            await h.executor.run(spec(h, thread, "first"))
            constrain(h)

            def unused(*args):
                raise AssertionError("child does not consume historical messages")

            monkeypatch.setattr(frame, "bound_latest_step", unused)
            run = await h.executor.run(h.run_spec(thread=thread, runnable="parent"))
            assert run.status == "succeeded", run.error

    asyncio.run(scenario())


@pytest.mark.parametrize("skipped", [False, True])
def test_batched_tool_replies_remain_in_their_own_step_units(tmp_path, skipped):
    from toolang.execution.compaction import history_units

    tool = RecordingTool("lookup__read", output={"body": "result"})
    calls = tuple(
        ToolCall(f"lookup-{i}", f"lookup-{i}", tool.name, {}) for i in range(2)
    )
    if skipped:
        calls = (ToolCall("chdir", "chdir", "_toolang__chdir", {"path": "/"}), calls[1])
    h = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        tools={tool.name: tool},
        responses=[ModelCallResult(tool_calls=calls), reply("done")],
    )

    async def scenario():
        async with h:
            thread = h.threads.create(prefix=ThreadPrefix.TERM)
            run = await h.executor.run(spec(h, thread, "work"))
            assert run.status == "succeeded", run.error
            units = history_units(h.store, run)
            for call in calls:
                (unit,) = [
                    u
                    for u in units
                    if any(
                        isinstance(p, ToolCallPart)
                        and p.tool_call_id == call.tool_call_id
                        for m in u.messages
                        for p in m.parts
                    )
                ]
                replies = [
                    p.tool_call_id
                    for m in unit.messages
                    for p in m.parts
                    if isinstance(p, ToolResultPart)
                ]
                assert replies == [call.tool_call_id]

    asyncio.run(scenario())


@pytest.mark.parametrize("caller_scale,success", [(1, True), (2, False)])
def test_summary_publication_uses_the_caller_model_count(
    tmp_path, monkeypatch, caller_scale, success
):
    from toolang.execution import tokens

    h = seeded_harness(tmp_path)

    async def scenario():
        async with h:
            thread, _ = await seed(h)
            monkeypatch.setattr(
                tokens,
                "_MODEL_TOKEN_SCALES",
                {"test/scripted": caller_scale, "test/reducer": 1},
            )
            h.setup = replace(
                h.setup,
                compact=replace(h.setup.compact, trigger=11200, recent=1, summary=256),
            )
            summary = "fact " * 1500
            h.adapter._responses.extend(
                [reply(summary), reply("done")] if success else [reply(summary)] * 3
            )
            run = await h.executor.run(spec(h, thread, "continue"))
            assert run.status == ("succeeded" if success else "failed"), (
                h.store.resolve_error(run.error) if run.error else None
            )
            assert (h.store.get_thread(thread_id=thread).horizon is not None) == success
            (child,) = compact_runs(h, thread)
            model_steps = [
                s for s in h.store.list_steps(run_id=child.id) if s.kind == "model"
            ]
            assert len(model_steps) == (1 if success else 3)
            assert all(
                s.status == ("succeeded" if success else "failed") for s in model_steps
            )
            assert h.adapter.invocations[-1].model.ref == (
                "test/scripted" if success else "test/reducer"
            )

    asyncio.run(scenario())


def test_recent_target_yields_to_the_complete_caller_budget(tmp_path):
    h = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        responses=[
            reply("old " * 18000),
            reply("middle " * 850),
            reply("latest " * 120),
        ],
    )

    async def scenario():
        async with h:
            thread, latest = await seed(h)
            constrain(h, context=10000)
            h.setup = replace(
                h.setup,
                compact=replace(h.setup.compact, recent=5000, summary=256),
            )
            h.adapter._responses.extend([reply("Short facts.")] * 3 + [reply("done")])
            current = await h.executor.run(spec(h, thread, "current " * 500))
            assert current.status == "succeeded", (
                h.store.resolve_error(current.error) if current.error else None
            )
            result = RunHistory(h.store).get_compaction(thread)
            assert result is not None and result.result.end == latest
            caller = h.adapter.invocations[-1].call
            assert "latest " * 120 in str(caller.messages)
            assert "middle " * 850 not in str(caller.messages)
            assert InputEstimate().count(caller, None) <= 8000
            assert len(h.adapter.invocations) == 5

    asyncio.run(scenario())
