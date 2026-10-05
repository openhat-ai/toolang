"""Root self-exec adopts compatible code without adding Run ancestry."""

import asyncio
from hashlib import sha256

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
    RecordingTool,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message
from toolang.base.types.policy import AgentCeiling, RunLimits
from toolang.base.types.run import ModelCallResult, ModelUsage, ToolCall
from toolang.execution.events import StepEnd
from toolang.execution.records import ExecuteControlPayload
from toolang.plugin.toolsets.loading import load_tools
from toolang.execution.types import ThreadPrefix
from toolang.state.prepare import prepare_agent_state


def answer(text):
    return ModelCallResult(message=Message.assistant(text))


def tool(name, arguments):
    return ModelCallResult(tool_calls=(ToolCall(name, name, name, arguments),))


def test_child_update_sync_then_native_root_self_exec(tmp_path):
    source = """flow grow() -> Text:
  repeat 2 times:
    repeat 3 times:
      run evolve
      exec grow
      run: Must not resume.
    until: Must not evaluate.
  run: Must not reach.
agic evolve() -> Text:
  tools = me/*
  Update the root and synchronize it.
"""
    updated = "flow grow() -> Text:\n  run: New root.\n"
    me = load_tools(queries=("me/*",))
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        tools=me,
        responses=[
            tool(
                "me__update",
                {
                    "key": "agent.too",
                    "if_digest": sha256(source.encode()).hexdigest(),
                    "content": updated,
                },
            ),
            tool("me__sync", {}),
            answer("updated"),
            answer("done"),
        ],
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
            assert root.status == "succeeded", root.error
            assert root.output is not None
            assert harness.store.resolve_value(root.output.value) == "done"
            assert harness.published is not None
            (control,) = harness.store.list_run_controls(run_id=root.id, kind="execute")
            assert isinstance(control.payload, ExecuteControlPayload)
            assert control.payload.state == harness.published.revision
            assert control.payload.runnable == "flow:grow"
            steps = harness.store.list_steps(run_id=root.id)
            assert [s.kind for s in steps] == ["loop", "loop", "run", "exec", "run"]
            assert all(s.status == "succeeded" for s in steps)
            assert [s.aborted_by for s in steps[:2]] == [control.ref, control.ref]
            assert len({s.ref for s in steps}) == len(steps)
            tree = harness.store.list_run_tree(root_run_id=root.id)
            assert len(tree) == 3
            assert all(r.parent.run_id == root.id for r in tree if r.parent)
            receipt = last_tool_result(harness.adapter.invocations[2].call)
            assert receipt.error is None
            assert receipt.output == {
                "revision": harness.published.revision,
                "files": [f.to_data() for f in harness.published.files],
            }
            assert "Update the root" in str(
                harness.adapter.invocations[2].call.messages
            )
            assert "New root." in str(harness.adapter.invocations[3].call.messages)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("unnamed", [False, True])
def test_model_root_self_exec_keeps_one_run(tmp_path, changed, unnamed):
    reference = "_" if unnamed else "grow"
    source = (
        f"agic{'' if unnamed else ' grow'}() -> Text:\n  handoffs = *\n  Old root.\n"
    )
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[
            ScriptedModelTurn(
                tool("_toolang__exec", {"runnable": reference}), gate=gate
            ),
            answer("done"),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="<entry:1>" if unnamed else "grow",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), 5)
            if changed:
                harness.setup.layout.program.write_text(
                    source.replace("Old root", "New root")
                )
                harness.published = prepare_agent_state(harness.setup.layout)
            gate.release()
            root = await handle
            assert root.status == "succeeded", root.error
            assert harness.store.list_run_tree(root_run_id=root.id) == [root]
            (control,) = harness.store.list_run_controls(run_id=root.id, kind="execute")
            assert isinstance(control.payload, ExecuteControlPayload)
            assert (
                control.payload.state == (harness.published or harness.state).revision
            )
            routes = route_snapshots(harness.adapter.invocations[0].call)
            assert routes["hands"] == []
            assert len(routes["handoffs"]) == 1
            assert ("New root." if changed else "Old root.") in str(
                harness.adapter.invocations[-1].call.messages
            )

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


@pytest.mark.parametrize("change", ["input", "output", "kind", "struct", "missing"])
def test_new_catalog_cannot_bypass_outgoing_root_contract(tmp_path, change):
    source = """struct Item:
  text: Text
agic grow(item: Item) -> Text:
  handoffs = *
  Old root.
"""
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[
            ScriptedModelTurn(tool("_toolang__chdir", {"path": "home://"}), gate=gate),
            tool(
                "_toolang__exec",
                {"runnable": "grow", "input": {"item": {"text": "work"}}},
            ),
            answer("recovered"),
        ],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="grow",
                    named={"item": {"text": "start"}},
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 5)
            updated = {
                "input": source.replace("item: Item", "item: Item, extra: Text"),
                "output": source.replace("-> Text", "-> Number"),
                "kind": source.replace("agic grow", "flow grow")
                .replace("  handoffs = *\n", "")
                .replace("  Old root.", "  run: Changed."),
                "struct": source.replace("text: Text", "text: Number"),
                "missing": source.replace("grow", "renamed"),
            }[change]
            harness.setup.layout.program.write_text(updated)
            harness.published = prepare_agent_state(harness.setup.layout)
            gate.release()
            root = await handle
            assert root.status == "succeeded", root.error
            result = last_tool_result(harness.adapter.invocations[-1].call)
            assert result.error is not None
            if change != "missing":
                assert "signature changed" in result.error
                assert harness.state.revision in result.error
                assert harness.published.revision in result.error
            assert not harness.store.list_run_controls(run_id=root.id, kind="execute")
            assert "Old root." in str(harness.adapter.invocations[-1].call.messages)

    asyncio.run(scenario())


def test_root_self_exec_still_requires_handoff_authorization(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic grow():\n  hands = *\n  handoffs = none\n  Work.\n",
        responses=[tool("_toolang__exec", {"runnable": "grow"}), answer("recovered")],
    )

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="grow",
                )
            )
            assert root.status == "succeeded"
            assert route_snapshots(harness.adapter.invocations[0].call) == {
                "hands": [],
                "handoffs": [],
            }
            assert (
                last_tool_result(harness.adapter.invocations[-1].call).error is not None
            )
            assert not harness.store.list_run_controls(run_id=root.id, kind="execute")

    asyncio.run(scenario())


@pytest.mark.parametrize("timing", ["immediate", "next_step", "next_call"])
def test_unchanged_native_self_exec_remains_cancelable(tmp_path, monkeypatch, timing):
    harness = ExecutionHarness.create(
        tmp_path, source="flow grow():\n  exec grow\n", responses=[]
    )
    tracer = RecordingRunTracer()

    async def scenario():
        scheduled = False

        async def on_event(event):
            nonlocal scheduled
            await RecordingRunTracer.on_event(tracer, event)
            if isinstance(event, StepEnd) and event.kind == "exec" and not scheduled:
                scheduled = True
                asyncio.get_running_loop().call_soon(
                    lambda: handle.cancel(timing=timing)
                )

        monkeypatch.setattr(tracer, "on_event", on_event)
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="grow",
                ),
                tracer=tracer,
            )
            root = await asyncio.wait_for(handle, 5)
            assert root.status == "canceled"
            assert (
                len(harness.store.list_run_controls(run_id=root.id, kind="execute"))
                == 1
            )

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_self_exec_preserves_root_budget_and_authority(tmp_path):
    blocked = RecordingTool("web__search", output={})
    response = ModelCallResult(
        tool_calls=(ToolCall("exec", "exec", "_toolang__exec", {"runnable": "grow"}),),
        usage=ModelUsage(input_tokens=4, output_tokens=2),
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic grow():\n  tools = *\n  handoffs = *\n  Work.\n",
        tools={blocked.name: blocked},
        responses=[response, response],
    )

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="grow",
                    limits=RunLimits(tokens=10, agic_model_calls=1),
                    ceilings=(AgentCeiling(tools=()),),
                )
            )
            assert root.status == "failed"
            assert root.error is not None
            assert "token" in harness.store.resolve_error(root.error).lower()
            assert len(harness.adapter.invocations) == 2
            assert (
                len(harness.store.list_run_controls(run_id=root.id, kind="execute"))
                == 1
            )
            assert all(
                blocked.name not in {tool.name for tool in call.call.tools}
                for call in harness.adapter.invocations
            )
            assert not blocked.calls

    asyncio.run(scenario())


def test_root_self_exec_rejects_while_a_descendant_is_active(tmp_path):
    from toolang.base.errors import ToolangError
    from toolang.execution.runnables import resolve_call_target

    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="flow grow() -> Text:\n  run child\nagic child() -> Text:\n  Wait.\n",
        responses=[ScriptedModelTurn(answer("done"), gate=gate)],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="grow",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 5)
            execution = harness.executor._active[handle.run_id].execution
            assert execution is not None
            root = execution._active_bindings[handle.run_id]
            target = resolve_call_target(root.state, root.module, "grow")
            assert not execution.can_self_exec(root, target)
            with pytest.raises(ToolangError, match="current or an ancestor"):
                execution.require_inactive_runnable(root, target, action="exec")
            gate.release()
            assert (await handle).status == "succeeded"

    asyncio.run(scenario())
