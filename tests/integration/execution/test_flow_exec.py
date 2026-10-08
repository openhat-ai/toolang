"""Native handoffs retain Run identity and atomically leave repeat bodies."""

import asyncio

import pytest

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, message_text
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.events import StepEnd, run_event_from_data, run_event_to_data
from toolang.execution.records import ExecControlPayload, RunControlPayload
from toolang.execution.types import ExecStepNoted, LoopStepNoted, ThreadPrefix, TypedRef
from toolang.state.prepare import prepare_agent_state


def answer(text):
    return ModelCallResult(message=Message.assistant(text))


@pytest.mark.parametrize("kind", ["flow", "agic"])
def test_repeated_handoffs_do_not_reload_step_history(tmp_path, monkeypatch, kind):
    count = 20
    harness = ExecutionHarness.create(
        tmp_path,
        source="\n".join(
            [
                f"{kind} stage_{i}(_: Text):\n"
                + (
                    f"  exec stage_{i + 1}"
                    if kind == "flow"
                    else "  handoffs = *\n  Continue."
                )
                for i in range(count)
            ]
            + [f"flow stage_{count}(_: Text):\n  pass"]
        ),
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        str(i),
                        str(i),
                        "_toolang__exec",
                        {"runnable": f"stage_{i + 1}", "input": {"_": "work"}},
                    ),
                )
            )
            for i in range(count)
        ]
        if kind == "agic"
        else [],
    )
    list_steps = harness.store.list_steps
    loaded = 0

    def track_reads(**kwargs):
        nonlocal loaded
        steps = list_steps(**kwargs)
        loaded += len(steps)
        return steps

    monkeypatch.setattr(harness.store, "list_steps", track_reads)

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="stage_0",
                    named={"_": "work"},
                )
            )
            assert root.status == "succeeded", (
                harness.store.resolve_error(root.error) if root.error else None
            )
            steps = list_steps(run_id=root.id)
            assert len(steps) == count * (2 if kind == "agic" else 1)
            assert [step.index for step in steps] == list(range(len(steps)))
            # Bound history reads by work performed, without timing assertions.
            assert loaded <= len(steps)

    asyncio.run(scenario())


@pytest.mark.parametrize("timing", ["immediate", "next_step", "next_call"])
def test_flow_handoffs_allow_scheduled_cancellation(tmp_path, monkeypatch, timing):
    harness = ExecutionHarness.create(
        tmp_path,
        source="\n".join(
            [f"flow stage_{i}(_: Text):\n  exec stage_{i + 1}" for i in range(10)]
            + ["flow stage_10(_: Text):\n  pass"]
        ),
        responses=[],
    )
    tracer = RecordingRunTracer()
    harness.intercept_events(tracer)

    async def scenario():
        scheduled = False

        def cancel():
            if not handle.task.done():
                handle.cancel(timing=timing)

        async def on_event(event):
            nonlocal scheduled
            await RecordingRunTracer.on_event(tracer, event)
            if isinstance(event, StepEnd) and event.kind == "exec" and not scheduled:
                scheduled = True
                asyncio.get_running_loop().call_soon(cancel)

        monkeypatch.setattr(tracer, "on_event", on_event)
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="stage_0",
                    named={"_": "work"},
                ),
                tracer=tracer,
            )
            root = await handle
            assert scheduled
            assert root.status == "canceled", (
                harness.store.resolve_error(root.error) if root.error else None
            )
            controls = harness.store.list_run_controls(run_id=root.id, kind="exec")
            assert len(controls) == 1
            assert harness.store.list_steps(run_id=root.id)[0].status == "succeeded"

    asyncio.run(scenario())


@pytest.mark.parametrize("target", ["successor", "-> Text: Continue {{_}}."])
def test_exec_records_only_consumed_inputs(tmp_path, target):
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
flow grow(_: Text, unused: Text) -> Text:
  exec {target}
agic successor(_: Text) -> Text:
  Continue {{{{_}}}}.
""",
        responses=[answer("done")],
    )

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="grow",
                    named={"_": "work", "unused": "unrelated"},
                )
            )
            assert root.status == "succeeded", (
                harness.store.resolve_error(root.error) if root.error else None
            )
            step = harness.store.list_steps(run_id=root.id)[0]
            assert step.kind == "exec"
            assert [
                harness.store.resolve_value(TypedRef(ref, "Text")) for ref in step.input
            ] == ["work"]

    asyncio.run(scenario())


def test_nested_exec_closes_only_own_repeats_and_parent_resumes(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent() -> Text:
  repeat 1 time:
    run grow
  run finish
flow grow() -> Text:
  repeat 3 times:
    repeat 2 times:
      exec successor
      run: Must not resume.
    until: Must not evaluate.
  run: Must not resume either.
flow successor() -> Text:
  run: Successor.
agic finish() -> Text:
  user: Parent continues.
""",
        responses=[answer("successor"), answer("finished")],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                ),
                tracer=tracer,
            )
            assert root.status == "succeeded", (
                harness.store.resolve_error(root.error) if root.error else None
            )
            assert root.output is not None
            assert harness.store.resolve_value(root.output.value) == "finished"
            grow = next(
                r
                for r in harness.store.list_run_tree(root_run_id=root.id)
                if (entry := harness.store.get_run_control(run_id=r.id, index=0))
                is not None
                and isinstance(entry.payload, RunControlPayload)
                and entry.payload.runnable == "flow:grow"
            )
            steps = harness.store.list_steps(run_id=grow.id)
            assert [s.kind for s in steps] == ["loop", "loop", "exec", "run"]
            (control,) = harness.store.list_run_controls(run_id=grow.id, kind="exec")
            assert control.triggered_by == steps[2].ref
            assert steps[2].noted == ExecStepNoted(control.ref, "flow:successor")
            assert steps[2].output is None
            for step, total in zip(steps[:2], [3, 2]):
                assert step.status == "succeeded"
                assert step.noted == LoopStepNoted(0, "exec", total)
                assert step.aborted_by == control.ref
            assert steps[3].ref.indices == (1,)
            ends = [
                e.step
                for e in tracer.events
                if isinstance(e, StepEnd) and e.step.run_id == grow.id
            ]
            assert ends == [s.ref for s in (steps[2], steps[1], steps[0], steps[3])]
            assert all(
                run_event_from_data(run_event_to_data(event)) == event
                for event in tracer.events
            )
            parent_loop = harness.store.list_steps(run_id=root.id)[0]
            assert parent_loop.noted == LoopStepNoted(1, "exhausted", 1)
            assert parent_loop.aborted_by is None
            assert len(harness.adapter.invocations) == 2

    asyncio.run(scenario())


def test_exec_uses_newly_published_target_and_typed_input(tmp_path):
    gate = AsyncGate()
    source = """
flow grow(_: Text) -> Text:
  handoffs = *
  let run pause
  exec successor
agic pause() -> Text:
  user: Wait for publication.
agic successor(_: Text) -> Text:
  user: Old target {{_}}.
"""
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[ScriptedModelTurn(answer("ready"), gate=gate), answer("done")],
    )

    async def scenario():
        async with harness:
            spec = harness.run_spec(
                thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                runnable="grow",
                named={"_": "work"},
            )
            handle = harness.executor.run(spec)
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            harness.setup.layout.program.write_text(
                "instruct evolved:\n  GENERATION = 2\n"
                + source.replace(
                    "  user: Old target {{_}}.",
                    "  instruct = evolved\n  context = none\n  user: New target {{_}}.",
                )
            )
            harness.published = prepare_agent_state(harness.setup.layout)
            gate.release()
            root = await handle
            assert root.status == "succeeded", (
                harness.store.resolve_error(root.error) if root.error else None
            )
            (control,) = harness.store.list_run_controls(run_id=root.id, kind="exec")
            assert isinstance(control.payload, ExecControlPayload)
            assert control.payload.state == harness.published.revision
            assert set(control.payload.input) == {"_"}
            pointer = control.payload.input["_"]
            assert isinstance(pointer, TypedRef) and pointer.type == "Text"
            assert harness.store.resolve_value(pointer) == "work"
            call = harness.adapter.invocations[-1].call
            assert "GENERATION = 2" in str(call)
            assert any(
                "New target work." in message_text(m.parts) for m in call.messages
            )
            assert harness.store.list_steps(run_id=root.id)[-1].state == control.ref

    asyncio.run(scenario())


def test_rejected_child_self_exec_leaves_no_handoff(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="flow parent():\n  run grow\nflow grow():\n  repeat 2 times:\n    exec grow\n",
        responses=[],
    )

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                )
            )
            assert root.status == "failed"
            assert root.error is not None
            assert "current or an ancestor" in str(
                harness.store.resolve_error(root.error)
            )
            assert not harness.store.list_run_controls(run_id=root.id, kind="exec")
            child = next(
                r for r in harness.store.list_run_tree(root_run_id=root.id) if r.parent
            )
            assert not harness.store.list_run_controls(run_id=child.id, kind="exec")
            steps = harness.store.list_steps(run_id=child.id)
            assert [step.status for step in steps] == ["failed", "failed"]
            assert all(step.aborted_by is None for step in steps)
            assert not harness.adapter.invocations

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["commit", "cancel", "delivery"])
def test_exec_closure_is_atomic_and_postcommit_failures_do_not_undo_it(
    tmp_path, monkeypatch, failure
):
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow grow() -> Text:
  repeat 3 times:
    repeat 2 times:
      exec successor
agic successor() -> Text:
  user: Successor.
""",
        responses=[answer("done")],
    )
    tracer = RecordingRunTracer()
    harness.intercept_events(tracer)
    original_finish = harness.store.finish_step
    injected = False

    def finish(**kwargs):
        nonlocal injected
        if failure == "commit" and kwargs.get("aborted_by") and not injected:
            injected = True
            raise RuntimeError("injected commit failure")
        return original_finish(**kwargs)

    async def on_event(event):
        nonlocal injected
        await RecordingRunTracer.on_event(tracer, event)
        if (
            isinstance(event, StepEnd)
            and event.kind == "exec"
            and event.status == "succeeded"
        ):
            # Observers cannot see a control with half-open old repeat records.
            steps = harness.store.list_steps(run_id=event.step.run_id)
            assert all(s.status == "succeeded" for s in steps)
            assert (
                len(
                    harness.store.list_run_controls(
                        run_id=event.step.run_id, kind="exec"
                    )
                )
                == 1
            )
            if failure != "commit" and not injected:
                injected = True
                if failure == "cancel":
                    raise asyncio.CancelledError()
                raise RuntimeError("injected delivery failure")

    async def scenario():
        async with harness:
            monkeypatch.setattr(harness.store, "finish_step", finish)
            monkeypatch.setattr(tracer, "on_event", on_event)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="grow",
                ),
                tracer=tracer,
            )
            assert injected
            steps = harness.store.list_steps(run_id=root.id)
            transfers = harness.store.list_run_controls(run_id=root.id, kind="exec")
            assert (
                root.status
                == {"commit": "failed", "cancel": "canceled", "delivery": "succeeded"}[
                    failure
                ]
            )
            if failure == "commit":
                assert not transfers
                assert [s.status for s in steps] == ["failed"] * 3
                assert all(s.aborted_by is None for s in steps)
            else:
                assert len(transfers) == 1
                assert [s.status for s in steps[:3]] == ["succeeded"] * 3
                assert [s.aborted_by for s in steps[:2]] == [transfers[0].ref] * 2
            from tests.support.execution_assertions import assert_run_event_integrity

            assert_run_event_integrity(tracer.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("empty", [False, True])
def test_repeated_handoffs_have_run_aligned_boundaries_and_reset_display_numbers(
    tmp_path, empty
):
    from toolang.cli.common.execution_progress import ProgressProjector
    from toolang.execution.inspection.trees import build_execution_tree, tree_to_data

    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow grow():
  repeat 2 times:
    exec second
flow second():
  exec final
flow final():
"""
        + ("  pass\n" if empty else "  run: Final.\n"),
        responses=[] if empty else [answer("done")],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="grow",
                ),
                tracer=tracer,
            )
            assert root.status == ("failed" if empty else "succeeded"), root.error
            projector = ProgressProjector(show_boundaries=True)
            rows = [
                row
                for event in tracer.events
                for block in projector.handle(event).committed
                for row in block.rows
            ]
            markers = [row for row in rows if row.leader == "handoff"]
            assert [row.text for row in markers] == [
                "exec → flow:second",
                "exec → flow:final",
            ]
            assert all(not row.prefix for row in markers)
            assert any("Handed off" in row.text for row in rows)
            assert not any("diagnostic" in row.text.lower() for row in rows)
            if not empty:
                assert any(row.text.startswith("[0] Run") for row in rows)
            tree = build_execution_tree(
                harness.store.load_execution_snapshot(root=root.id)
            )
            assert len(tree.handoffs) == 2
            assert all(boundary.depth == 0 for boundary in tree.handoffs)
            assert [boundary.runnable for boundary in tree.handoffs] == [
                "flow:second",
                "flow:final",
            ]
            steps = harness.store.list_steps(run_id=root.id)
            assert tree.step_ordinal(steps[2]) == 0
            if not empty:
                assert tree.step_ordinal(steps[-1]) == 0
                assert steps[-1].ref.indices == (2,)
            assert len({s.ref for s in steps}) == len(steps)
            assert (
                sum(
                    len(boundaries)
                    for node in tree_to_data(tree)
                    if isinstance(boundaries := node.get("handoffs_after"), list)
                )
                == 2
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("self_exec", [False, True])
def test_flow_exec_preserves_original_output_contract(tmp_path, self_exec):
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow grow() -> Number:
  exec successor
agic successor() -> Text:
  user: Successor.
""",
        responses=(
            [
                ModelCallResult(
                    tool_calls=(
                        ToolCall(
                            "restart",
                            "restart",
                            "_toolang__exec",
                            {"runnable": "successor"},
                        ),
                    )
                )
            ]
            if self_exec
            else []
        )
        + [answer("not a number")],
    )

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="grow",
                )
            )
            assert root.status == "failed"
            assert len(
                harness.store.list_run_controls(run_id=root.id, kind="exec")
            ) == (2 if self_exec else 1)
            assert harness.store.list_steps(run_id=root.id)[0].status == "succeeded"

    asyncio.run(scenario())


def test_exec_honors_next_call_cancellation_before_commit(tmp_path):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow grow() -> Text:
  run pause
  exec successor
agic pause() -> Text:
  user: Pause.
agic successor() -> Text:
  user: Must not start.
""",
        responses=[ScriptedModelTurn(answer("ready"), gate=gate)],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="grow",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            handle.cancel(timing="next_call")
            gate.release()
            root = await handle
            assert root.status == "canceled"
            assert not harness.store.list_run_controls(run_id=root.id, kind="exec")
            assert len(harness.adapter.invocations) == 1
            assert harness.store.list_steps(run_id=root.id)[-1].status == "canceled"

    asyncio.run(scenario())


def test_exec_preserves_resource_ceiling_and_root_token_accounting(tmp_path):
    from tests.support.execution_harness import RecordingTool
    from dataclasses import replace
    from toolang.base.types.policy import AgentCeiling, RunLimits
    from toolang.base.types.run import ModelUsage

    tool = RecordingTool("web__search", output={"result": "unused"})
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow grow() -> Text:
  tools = none
  run pause
  exec successor
agic pause() -> Text:
  user: Pause.
agic successor() -> Text:
  tools = *
  user: Successor.
""",
        tools={tool.name: tool},
        responses=[
            ModelCallResult(
                message=Message.assistant("ready"),
                usage=ModelUsage(input_tokens=4, output_tokens=2),
            ),
            ModelCallResult(
                message=Message.assistant("done"),
                usage=ModelUsage(input_tokens=3, output_tokens=2),
            ),
        ],
    )

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                replace(
                    harness.run_spec(
                        thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                        runnable="grow",
                        limits=RunLimits(tokens=10),
                    ),
                    ceilings=(AgentCeiling(tools=()),),
                )
            )
            assert root.status == "failed"
            assert root.error is not None
            assert "token" in harness.store.resolve_error(root.error).lower()
            assert (
                len(harness.store.list_run_controls(run_id=root.id, kind="exec")) == 1
            )
            assert len(harness.adapter.invocations) == 2
            assert all(
                tool.name not in {t.name for t in i.call.tools}
                for i in harness.adapter.invocations
            )
            assert not tool.calls

    asyncio.run(scenario())


def test_inline_exec_retains_owning_code_after_lines_move(tmp_path):
    source = """
instruct: GENERATION = 1
agic pause() -> Text:
  Wait.
flow grow(_: Text) -> Text:
  context = none
  let run pause
  exec -> Text: Old inline {{_}}.
"""
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[ScriptedModelTurn(answer("ready"), gate=gate), answer("done")],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="grow",
                    named={"_": "work"},
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            harness.setup.layout.program.write_text(
                "\n\n"
                + source.replace("Old inline", "New inline").replace(
                    "GENERATION = 1", "GENERATION = 2"
                )
            )
            harness.published = prepare_agent_state(harness.setup.layout)
            gate.release()
            root = await handle
            assert root.status == "succeeded", (
                harness.store.resolve_error(root.error) if root.error else None
            )
            (control,) = harness.store.list_run_controls(run_id=root.id, kind="exec")
            assert isinstance(control.payload, ExecControlPayload)
            assert control.payload.state == harness.state.revision
            call = harness.adapter.invocations[-1].call
            assert "Old inline work." in str(call.messages)
            assert "New inline" not in str(call.messages)
            assert "GENERATION = 2" in call.instructions

    asyncio.run(scenario())


def test_evolution_cycle_adopts_new_grow_without_adding_run_frames(tmp_path):
    source = """
flow grow() -> Text:
  exec evolve
flow evolve() -> Text:
  run: Update grow.
  exec grow
"""
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[ScriptedModelTurn(answer("updated"), gate=gate), answer("done")],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="grow",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            harness.setup.layout.program.write_text(
                source.replace("  exec evolve", "  run: New grow.")
            )
            harness.published = prepare_agent_state(harness.setup.layout)
            gate.release()
            root = await handle
            assert root.status == "succeeded", (
                harness.store.resolve_error(root.error) if root.error else None
            )
            controls = harness.store.list_run_controls(run_id=root.id, kind="exec")
            assert [
                (c.payload.runnable, c.payload.state)
                for c in controls
                if isinstance(c.payload, ExecControlPayload)
            ] == [
                ("flow:evolve", harness.state.revision),
                ("flow:grow", harness.published.revision),
            ]
            runs = harness.store.list_run_tree(root_run_id=root.id)
            assert len(runs) == 3
            assert all(r.parent.run_id == root.id for r in runs if r.parent)
            assert "New grow." in str(harness.adapter.invocations[-1].call.messages)

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["missing", "signature"])
def test_changed_exec_target_rejects_before_commit(tmp_path, change):
    source = """
flow grow() -> Text:
  run pause
  exec successor
agic pause() -> Text:
  Wait.
agic successor() -> Text:
  Successor.
"""
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[ScriptedModelTurn(answer("ready"), gate=gate)],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="grow",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            replacement = source.replace("  exec successor\n", "")
            replacement = (
                replacement.replace("agic successor() -> Text:\n  Successor.\n", "")
                if change == "missing"
                else replacement.replace("successor() -> Text", "successor() -> Number")
            )
            harness.setup.layout.program.write_text(replacement)
            harness.published = prepare_agent_state(harness.setup.layout)
            gate.release()
            root = await handle
            assert root.status == "failed" and root.error is not None
            error = harness.store.resolve_error(root.error)
            assert (
                harness.state.revision in error and harness.published.revision in error
            )
            assert not harness.store.list_run_controls(run_id=root.id, kind="exec")
            step = harness.store.list_steps(run_id=root.id)[-1]
            assert step.kind == "exec" and step.status == "failed"
            assert step.state == root.state and step.noted is None
            assert len(harness.adapter.invocations) == 1

    asyncio.run(scenario())
