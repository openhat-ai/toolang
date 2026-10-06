"""Repeat conditions execute at their authored position without binding results."""

import asyncio

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
from toolang.base.types.message import Message, TextPart, message_text
from toolang.base.types.run import ModelCallResult
from toolang.common.layout import AgentLayout
from toolang.execution.events import StepEnd
from toolang.execution.executor.executor import _Execution
from toolang.execution.types import LoopStepNoted, RunHandle, ThreadPrefix
from toolang.state.prepare import prepare_agent_state


def _answer(text):
    return ModelCallResult(message=Message.assistant(text))


def _source(index, target="inline", count="3 times", condition=None):
    statements = ["run before", "run after"]
    condition = (
        condition
        or {
            "inline": "until: condition {{_}}",
            "agic": "until ready",
            "flow": "until check_flow",
        }[target]
    )
    statements.insert(index, condition)
    return (
        """agic before(_: Text) -> Text:
  recall = none
  before {{_}}
agic after(_: Text) -> Text:
  recall = none
  after {{_}}
agic ready(_: Text) -> Boolean:
  recall = none
  condition {{_}}
flow check_flow(_: Text) -> Boolean:
  run ready
flow main(_: Text) -> Text:
  recall = none
  context = none
  instruct = none
"""
        + f"  repeat {count}:\n"
        + "".join(f"    {line}\n" for line in statements)
    )


def _run(harness):
    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="main",
                    primary=(TextPart("seed"),),
                )
            )
            return (
                root,
                harness.store.list_steps(run_id=root.id),
                harness.store.list_run_tree(root_run_id=root.id),
                harness.store.run_output_text(run_id=root.id) if root.output else None,
                harness.store.resolve_error(root.error) if root.error else None,
            )

    return asyncio.run(scenario())


@pytest.mark.parametrize("target", ["inline", "agic", "flow"])
@pytest.mark.parametrize(
    "index,responses,iterations,output,prompts",
    [
        (
            0,
            ["false", "p0", "s0", "true"],
            1,
            "s0",
            ["condition seed", "before seed", "after p0", "condition s0"],
        ),
        (
            1,
            ["p0", "false", "s0", "p1", "true"],
            1,
            "p1",
            ["before seed", "condition p0", "after p0", "before s0", "condition p1"],
        ),
        (
            2,
            ["p0", "s0", "false", "p1", "s1", "true"],
            2,
            "s1",
            [
                "before seed",
                "after p0",
                "condition s0",
                "before s0",
                "after p1",
                "condition s1",
            ],
        ),
    ],
)
def test_position_controls_order_count_and_retained_output(
    tmp_path, target, index, responses, iterations, output, prompts
):
    harness = ExecutionHarness.create(
        tmp_path,
        source=_source(index, target),
        responses=[_answer(v) for v in responses],
    )
    root, steps, runs, actual, error = _run(harness)
    assert root.status == "succeeded", error
    assert actual == output
    loop = next(step for step in steps if step.parent is None)
    assert loop.noted == LoopStepNoted(iterations, "satisfied", 3)
    body = [step for step in steps if step.parent == loop.ref]
    assert [step.ref.indices[-1] for step in body] == list(range(len(body)))
    assert all(step.occur.iteration.phase == "body" for step in body)
    checks = [run for run in runs if run.parent == loop.ref]
    assert [run.occur.iteration.index for run in checks] == [0, 1]
    assert all(run.occur.iteration.phase == "until" for run in checks)
    assert all(run.output.binding is None for run in checks)
    assert [
        message_text(without_runtime_snapshots(call.call.messages)[-1].parts)
        for call in harness.adapter.invocations
    ] == prompts


@pytest.mark.parametrize("index", [0, 1, 2])
def test_first_true_condition_skips_exactly_the_suffix(tmp_path, index):
    responses = [*(["p0", "s0"][:index]), "true"]
    harness = ExecutionHarness.create(
        tmp_path,
        source=_source(index, "agic", count=""),
        responses=[_answer(v) for v in responses],
    )
    root, steps, _, output, error = _run(harness)
    assert root.status == "succeeded", error
    assert output == ["seed", "p0", "s0"][index]
    assert steps[0].noted == LoopStepNoted(int(index == 2), "satisfied", None)
    assert len(harness.adapter.invocations) == index + 1


@pytest.mark.parametrize("index", [0, 1, 2])
def test_count_limit_does_not_add_a_condition_check(tmp_path, index):
    responses = ["p0", "s0"]
    responses.insert(index, "false")
    harness = ExecutionHarness.create(
        tmp_path,
        source=_source(index, "flow", count="1 time"),
        responses=[_answer(v) for v in responses],
    )
    root, steps, _, output, error = _run(harness)
    assert root.status == "succeeded", error
    assert output == "s0"
    assert steps[0].noted == LoopStepNoted(1, "exhausted", 1)
    assert len(harness.adapter.invocations) == 3


@pytest.mark.parametrize("index", [0, 1, 2])
def test_zero_count_makes_no_calls(tmp_path, index):
    harness = ExecutionHarness.create(
        tmp_path, source=_source(index, "agic", count="0 times"), responses=[]
    )
    root, steps, runs, output, error = _run(harness)
    assert root.status == "succeeded", error
    assert output == "seed" and len(runs) == 1
    assert steps[0].noted == LoopStepNoted(0, "exhausted", 0)
    assert not harness.adapter.invocations


@pytest.mark.parametrize("failure", [RuntimeError("condition failed"), "not a boolean"])
def test_failed_trailing_condition_does_not_count_body(tmp_path, failure):
    harness = ExecutionHarness.create(
        tmp_path,
        source=_source(2, "agic"),
        responses=[
            _answer("prefix"),
            _answer("suffix"),
            failure if isinstance(failure, Exception) else _answer(failure),
        ],
    )
    root, steps, _, _, error = _run(harness)
    assert root.status == "failed" and error is not None
    assert steps[0].noted == LoopStepNoted(0, "failed", 3)
    assert all(step.status == "succeeded" for step in steps[1:])


def test_canceled_trailing_condition_does_not_count_body(tmp_path):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=_source(2, "agic"),
        responses=[
            _answer("prefix"),
            _answer("suffix"),
            ScriptedModelTurn(_answer("true"), gate=gate),
        ],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="main",
                    primary=(TextPart("seed"),),
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            handle.cancel()
            root = await asyncio.wait_for(handle, 2)
            assert root.status == "canceled"
            assert harness.store.list_steps(run_id=root.id)[0].noted == LoopStepNoted(
                0, "canceled", 3
            )

    asyncio.run(scenario())


def test_unbounded_local_only_repeat_yields_to_cancellation(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="flow main():\n  repeat:\n    let value = Body.\n",
        responses=[],
    )

    async def scenario():
        async with harness:
            entered = asyncio.Event()

            class Tracer(RecordingRunTracer):
                async def on_event(self, event):
                    await super().on_event(event)
                    if isinstance(event, StepEnd) and event.kind == "value":
                        entered.set()

            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="main",
                ),
                tracer=Tracer(),
            )
            await asyncio.wait_for(entered.wait(), 2)
            handle.cancel()
            root = await asyncio.wait_for(handle, 2)
            assert root.status == "canceled"
            steps = harness.store.list_steps(run_id=root.id)
            noted = steps[0].noted
            assert isinstance(noted, LoopStepNoted)
            assert noted.iterations > 0
            assert noted.termination == "canceled"
            assert not harness.adapter.invocations

    asyncio.run(scenario())


@pytest.mark.parametrize("target", ["inline", "agic"])
def test_leading_history_condition_uses_only_previous_complete_passes(tmp_path, target):
    source = _source(0, target).replace(
        "condition {{_}}", "condition {{_1._}} current {{_}}"
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[_answer("prefix"), _answer("suffix"), _answer("true")],
    )
    root, steps, _, output, error = _run(harness)
    assert root.status == "succeeded", error
    assert output == "suffix"
    assert steps[0].noted == LoopStepNoted(1, "satisfied", 3)
    assert "condition suffix current suffix" in str(
        harness.adapter.invocations[-1].call.messages
    )


@pytest.mark.parametrize("guarded", [False, True])
def test_named_flow_has_no_recursive_history_warmup(tmp_path, guarded):
    body = "{{#_1}}{{_1._}}{{/_1}}" if guarded else "{{_1._}}"
    source = _source(0, "flow").replace("condition {{_}}", f"condition {body}")
    harness = ExecutionHarness.create(
        tmp_path, source=source, responses=[_answer("true")]
    )
    root, steps, _, output, error = _run(harness)
    assert root.status == ("succeeded" if guarded else "failed"), error
    assert steps[0].noted.iterations == 0
    assert len(harness.adapter.invocations) == int(guarded)
    if guarded:
        assert output == "seed"
    else:
        assert "_1" in str(error)


def test_condition_keeps_one_state_snapshot_across_template_checks_and_call(
    tmp_path, monkeypatch
):
    source = _source(2, "agic", count="1 time")
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[_answer("prefix"), _answer("suffix"), _answer("true")],
    )
    original = _Execution.condition_templates
    calls = 0

    def templates(execution, *args, **kwargs):
        nonlocal calls
        result = original(execution, *args, **kwargs)
        calls += 1
        if calls == 2:
            harness.setup.layout.program.write_text(
                source.replace("condition {{_}}", "New condition {{_}}")
            )
            harness.published = prepare_agent_state(harness.setup.layout)
        return result

    monkeypatch.setattr(_Execution, "condition_templates", templates)
    root, _, runs, _, error = _run(harness)
    assert root.status == "succeeded", error
    assert harness.published is not None
    assert "New condition" not in str(harness.adapter.invocations[-1].call.messages)
    assert any(
        run.occur and run.occur.iteration and run.occur.iteration.phase == "until"
        for run in runs
    )


@pytest.mark.parametrize("index", [0, 1, 2])
def test_changed_condition_signature_is_rejected_at_its_position(tmp_path, index):
    source = _source(index, "agic", count="1 time")
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[_answer(value) for value in ["prefix", "suffix"][:index]],
    )
    harness.setup.layout.program.write_text(
        source.replace(
            "agic ready(_: Text) -> Boolean", "agic ready(_: Text) -> Text"
        ).replace("until ready", "until: Done?")
    )
    harness.published = prepare_agent_state(harness.setup.layout)
    root, steps, _, _, error = _run(harness)
    assert root.status == "failed"
    assert "signature changed" in str(error)
    assert steps[0].noted.iterations == 0
    assert len(harness.adapter.invocations) == index


def test_nested_condition_exits_only_its_own_repeat(tmp_path):
    source = """flow main(_: Text):
  repeat 2 times:
    repeat:
      until: Stop inner.
      run: Skipped inner suffix.
    run: Outer body.
"""
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[_answer(v) for v in ["true", "first", "true", "second"]],
    )
    root, steps, _, output, error = _run(harness)
    assert root.status == "succeeded", error
    assert output == "second"
    assert [step.noted for step in steps if step.kind == "loop"] == [
        LoopStepNoted(2, "exhausted", 2),
        LoopStepNoted(0, "satisfied", None),
        LoopStepNoted(0, "satisfied", None),
    ]
    assert len(harness.adapter.invocations) == 4


def test_retry_restores_only_executed_prefix_effects_and_replays_records(tmp_path):
    source = """agic before(_: Text):
  Prefix.
agic ready() -> Boolean:
  Ready?
flow main(_: Text):
  let skipped = Original.
  repeat:
    let kept = run before
    until ready
    let skipped = Overwritten.
  run: kept={{kept}} skipped={{skipped}} primary={{_}}
"""
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[
            _answer("Committed."),
            _answer("true"),
            RuntimeError("retry final statement"),
            _answer("done"),
        ],
    )
    tracer = RecordingRunTracer()
    retry_tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="main",
                    primary=(TextPart("seed"),),
                ),
                tracer=tracer,
            )
            assert root.status == "failed"
            assert_replayed(harness.store.db_path, tracer.events)
            retried = await harness.executor.retry(
                root.id, setup=harness.setup, state=harness.state, tracer=retry_tracer
            )
            assert retried.status == "succeeded", retried.error
            steps = harness.store.list_steps(run_id=root.id)
            assert steps[1].noted == LoopStepNoted(0, "satisfied", None)
            assert len([step for step in steps if step.parent == steps[1].ref]) == 1
            assert harness.store.run_output_text(run_id=root.id) == "done"

    asyncio.run(scenario())
    assert len(harness.adapter.invocations) == 4
    for call in harness.adapter.invocations[-2:]:
        assert "kept=Committed. skipped=Original. primary=seed" in str(
            call.call.messages
        )
    assert_replayed(harness.store.db_path, retry_tracer.events)


def test_named_condition_rejects_active_runnable(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="flow main(_: Text) -> Boolean:\n  repeat:\n    until main\n    run: Body.\n",
        responses=[],
    )
    root, steps, runs, _, error = _run(harness)
    assert root.status == "failed"
    assert "current or an ancestor runnable: flow:main" in str(error)
    assert steps[0].noted == LoopStepNoted(0, "failed", None)
    assert len(runs) == 1 and not harness.adapter.invocations


@pytest.mark.parametrize("valid", [True, False])
def test_named_condition_binds_typed_prefix_inputs(tmp_path, valid):
    source = """agic measure() -> Number:
  Measure.
agic ready(value: Number) -> Boolean:
  Check {{value}}.
flow main(_: Text):
  repeat:
    let value = run measure
    until ready
    run: Skipped.
"""
    if not valid:
        source = source.replace("let value = run measure", "let value = invalid")
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[_answer("2"), _answer("true")] if valid else [],
    )
    root, steps, _, output, error = _run(harness)
    assert root.status == ("succeeded" if valid else "failed"), error
    assert steps[0].noted.iterations == 0
    if valid:
        assert output == "seed"
        assert "Check 2." in str(harness.adapter.invocations[-1].call.messages)
    else:
        assert "Number" in str(error)
        assert not harness.adapter.invocations


def test_conditionally_available_input_is_checked_at_runtime(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="""agic ready(value: Text) -> Boolean:
  Check {{value}}.
flow main(_: Text):
  repeat:
    repeat:
      until: Exit before assignment.
      let value = Created.
    until ready
""",
        responses=[_answer("true")],
    )
    root, _, _, _, error = _run(harness)
    assert root.status == "failed"
    assert "value" in str(error)
    assert len(harness.adapter.invocations) == 1


def test_exec_inside_named_flow_condition_stays_in_child_run(tmp_path):
    source = _source(0, "flow").replace("  run ready", "  exec ready")
    harness = ExecutionHarness.create(
        tmp_path, source=source, responses=[_answer("true")]
    )

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="main",
                    primary=(TextPart("seed"),),
                )
            )
            assert root.status == "succeeded", root.error
            assert harness.store.run_output_text(run_id=root.id) == "seed"
            assert not harness.store.list_run_controls(run_id=root.id, kind="exec")
            child = harness.store.list_run_tree(root_run_id=root.id)[1]
            assert (
                len(harness.store.list_run_controls(run_id=child.id, kind="exec")) == 1
            )
            assert harness.store.list_steps(run_id=root.id)[0].noted == LoopStepNoted(
                0, "satisfied", 3
            )

    asyncio.run(scenario())


def test_unbounded_repeat_can_transfer_through_exec(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="""agic done(_: Text):
  Finish.
flow main(_: Text):
  repeat:
    exec done
  run: Unreachable.
""",
        responses=[_answer("finished")],
    )
    root, steps, runs, output, error = _run(harness)
    assert root.status == "succeeded", error
    assert output == "finished" and len(runs) == 1
    assert steps[0].noted.iterations == 0
    assert len(harness.adapter.invocations) == 1


@pytest.mark.parametrize("target", ["inline", "agic"])
def test_named_condition_retains_tools_but_inline_condition_disables_them(
    tmp_path, target
):
    tool = RecordingTool("test__check", output={"ok": True})
    harness = ExecutionHarness.create(
        tmp_path,
        source=_source(0, target),
        tools={tool.name: tool},
        responses=[_answer("true")],
    )
    root, _, _, _, error = _run(harness)
    assert root.status == "succeeded", error
    names = {tool.name for tool in harness.adapter.invocations[0].call.tools}
    assert ("test__check" in names) == (target == "agic")
    if target == "inline":
        assert not names


def test_module_condition_uses_its_own_settings_for_history_warmup(tmp_path):
    source = """instruct check:
  Wrong module instruction.
flow main(_: Text):
  instruct = check
  run research
"""
    layout = AgentLayout.resident(tmp_path, "alice")
    (layout.home / "flows").mkdir(parents=True)
    layout.program.write_text(source)
    (layout.home / "flows/research.too").write_text("""instruct check:
  Previous={{_1._}}.
agic ready(_: Text) -> Boolean:
  instruct = check
  context = none
  Ready?
flow(_: Text):
  repeat:
    until ready
    let value = Body.
""")
    state = prepare_agent_state(layout)
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        program=state.modules["agent"],
        state=state,
        responses=[_answer("true")],
    )
    root, _, _, output, error = _run(harness)
    assert root.status == "succeeded", error
    assert output == "seed"
    assert len(harness.adapter.invocations) == 1
    assert "Previous=seed." in harness.adapter.invocations[0].call.instructions
    assert "Wrong module" not in harness.adapter.invocations[0].call.instructions


@pytest.mark.parametrize("prefix", ["", "    let note = Before condition.\n"])
def test_until_refreshes_handle_status_on_each_evaluation(tmp_path, prefix):
    worker_gate, body_gate = AsyncGate(), AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=(
            "agic worker():\n  Worker.\n"
            "flow main(_: Text):\n  context = none\n  instruct = none\n"
            "  let job = spawn worker\n  repeat 2 times:\n"
            + prefix
            + "    until: status={{job.status}}\n    run: Wait in body.\n"
        ),
        responses=[
            ScriptedModelTurn(_answer("finished"), gate=worker_gate),
            _answer("false"),
            ScriptedModelTurn(_answer("body"), gate=body_gate),
            _answer("true"),
        ],
    )

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if isinstance(event, StepEnd) and event.kind == "spawn":
                await asyncio.wait_for(worker_gate.wait_until_entered(), 2)

    tracer = Tracer()

    async def scenario():
        async with harness:
            parent = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="main",
                    primary=(TextPart("seed"),),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(body_gate.wait_until_entered(), 2)
            spawn = harness.store.list_steps(run_id=parent.run_id)[0]
            assert spawn.output is not None and isinstance(
                spawn.output.value, RunHandle
            )
            job = spawn.output.value
            worker = harness.executor._active[job.id].task
            worker_gate.release()
            assert (await asyncio.wait_for(worker, 2)).status == "succeeded"
            assert harness.store.run_handle_view(job)["status"] == "succeeded"
            body_gate.release()
            root = await asyncio.wait_for(parent, 2)
            assert root.status == "succeeded", root.error
            assert harness.store.list_steps(run_id=root.id)[1].noted == LoopStepNoted(
                1, "satisfied", 2
            )

    asyncio.run(scenario())
    condition_prompts = [
        message_text(without_runtime_snapshots(call.call.messages)[-1].parts)
        for call in harness.adapter.invocations[1::2]
    ]
    assert condition_prompts == ["status=running", "status=succeeded"]
    assert_replayed(harness.store.db_path, tracer.events)
