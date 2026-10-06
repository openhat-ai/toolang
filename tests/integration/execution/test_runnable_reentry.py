"""Reject active-path calls without treating siblings or module names as recursion."""

import asyncio

import pytest

from tests.support.execution_assertions import (
    assert_replayed,
    last_tool_result,
    route_snapshots,
)
from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.common.layout import AgentLayout
from toolang.execution.events import StepBegin
from toolang.execution.types import ThreadPrefix
from toolang.state.prepare import prepare_agent_state


@pytest.mark.parametrize(
    ("statement", "ancestor"),
    [
        (statement, ancestor)
        for statement in [
            "run outer",
            "exec outer",
            "map in 2 lanes using outer",
            "generate 2 in 2 lanes using outer",
            "reduce using outer",
            "keep if outer",
            "drop if outer",
            "sort ascending by outer",
        ]
        for ancestor in [False, True]
        if statement != "exec outer" or ancestor
    ],
)
def test_flow_statements_reject_active_targets_even_after_publication(
    tmp_path, statement, ancestor
):
    operation = statement.split()[0]
    input_type = "Text[]" if operation == "gather" else "Text"
    output_type = {
        "scatter": "Text[]",
        "keep": "Boolean",
        "drop": "Boolean",
        "sort": "Number",
    }.get(operation, "Text")
    declaration = f"flow outer(_: {input_type}) -> {output_type}:\n"
    body = f"  repeat 2 times:\n    {statement}\n"
    source = (
        "agic seed() -> Text[]:\n  Seed the collection.\n"
        + declaration
        + ("  run inner\nflow inner():\n" if ancestor else "")
        + "  run seed\n"
        + body
    )
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant('["a", "b"]')), gate=gate
            ),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="outer",
                    named={"_": ["seed"] if input_type == "Text[]" else "seed"},
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            harness.setup.layout.program.write_text(
                source.replace(
                    declaration,
                    declaration + "  run: New outer body must not run.\n",
                )
            )
            harness.published = prepare_agent_state(harness.setup.layout)
            assert harness.published.revision != harness.state.revision
            gate.release()
            root = await asyncio.wait_for(handle, 2)
            assert root.status == "failed"
            assert root.error is not None
            assert "current or an ancestor runnable: flow:outer" in (
                harness.store.resolve_error(root.error)
            )
            runs = harness.store.list_run_tree(root_run_id=root.id)
            assert len(runs) == (3 if ancestor else 2)
            assert len(harness.adapter.invocations) == 1
            for run in runs:
                assert not harness.store.list_run_controls(run_id=run.id, kind="exec")

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("action", ["run", "exec"])
@pytest.mark.parametrize("target", ["agic:worker", "flow:outer"])
def test_map_siblings_accept_distinct_revisions_but_reject_their_active_path(
    tmp_path, action, target
):
    source = """
agic seed() -> Text[]:
  Seed the collection.
agic worker(_: Text) -> Text:
  context = none
  hands = *
  handoffs = *
  Old worker body. Input: {{_}}
flow outer() -> Text[]:
  run seed
  map in 2 lanes using worker
"""
    gates = [AsyncGate(), AsyncGate()]
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[
            ModelCallResult(message=Message.assistant('["a", "b"]')),
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("first")), gate=gates[0]
            ),
            ScriptedModelTurn(
                ModelCallResult(
                    tool_calls=(
                        ToolCall(
                            "reentry",
                            "reentry",
                            f"_toolang__{action}",
                            {"runnable": target, "input": {"_": "again"}},
                        ),
                    )
                ),
                gate=gates[1],
            ),
            ModelCallResult(message=Message.assistant("recovered")),
        ],
    )

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if isinstance(event, StepBegin) and event.kind == "model":
                child = harness.store.get_run(run_id=event.step.run_id)
                assert child is not None and child.parent is not None
                if child.parent.indices == (1,) and harness.published is None:
                    # The first worker is already accepted; the sibling is not.
                    harness.setup.layout.program.write_text(
                        source.replace("Old worker body.", "New worker body.")
                    )
                    harness.published = prepare_agent_state(harness.setup.layout)

    tracer = Tracer()

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="outer",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(
                asyncio.gather(*(gate.wait_until_entered() for gate in gates)), 2
            )
            workers = [
                r
                for r in harness.store.list_run_tree(root_run_id=handle.run_id)
                if r.parent and r.parent.indices == (1,)
            ]
            assert len(workers) == 2
            assert all(r.status == "running" for r in workers)
            assert harness.published is not None
            assert {harness.store.resolve_state_revision(r.state) for r in workers} == {
                harness.state.revision,
                harness.published.revision,
            }
            assert "Old worker body." in str(
                harness.adapter.invocations[1].call.messages
            )
            assert "New worker body." in str(
                harness.adapter.invocations[2].call.messages
            )
            for gate in gates:
                gate.release()
            root = await asyncio.wait_for(handle, 2)
            assert root.status == "succeeded", root.error
            assert (
                harness.store.run_output_text(run_id=root.id) == '["first","recovered"]'
            )
            runs = harness.store.list_run_tree(root_run_id=root.id)
            assert len(runs) == 4
            assert len(harness.adapter.invocations) == 4
            result = last_tool_result(harness.adapter.invocations[-1].call)
            assert result.error == (
                f"_toolang/{action} cannot call the current or an ancestor runnable: {target}"
            )
            for invocation in harness.adapter.invocations[1:]:
                for routes in route_snapshots(invocation.call).values():
                    assert not {r["ref"] for r in routes} & {
                        "agic:worker",
                        "flow:outer",
                    }
            assert all(
                not harness.store.list_run_controls(run_id=r.id, kind="exec")
                for r in runs
            )

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_same_named_runnable_in_another_module_is_not_an_ancestor(tmp_path):
    source = """
agic worker() -> Text:
  hands = research
  context = none
  Delegate from the agent worker.
"""
    layout = AgentLayout.resident(tmp_path, "alice")
    (layout.home / "flows").mkdir(parents=True)
    layout.program.write_text(source)
    (layout.home / "flows/research.too").write_text("""
agic worker() -> Text:
  context = none
  Private module worker.
flow() -> Text:
  run worker
""")
    initial = prepare_agent_state(layout)
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        state=initial,
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "delegate",
                        "delegate",
                        "_toolang__run",
                        {"runnable": "research"},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("helped")),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="worker",
                )
            )
            assert root.status == "succeeded", root.error
            assert len(harness.store.list_run_tree(root_run_id=root.id)) == 3
            assert len(harness.adapter.invocations) == 3
            assert "Private module worker." in str(
                harness.adapter.invocations[1].call.messages
            )
            assert not last_tool_result(harness.adapter.invocations[2].call).error

    asyncio.run(scenario())
