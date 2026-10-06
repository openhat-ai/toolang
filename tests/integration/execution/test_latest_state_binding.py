"""Publication changes new named invocations, never accepted plans."""

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
    ScriptedModelTurn,
    RecordingRunTracer,
    RecordingTool,
)
from toolang.base.types.message import Message, message_text
from toolang.base.types.policy import AgentCeiling
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.records import RunControlPayload, StoredModelStepGiven
from toolang.execution.events import StepEnd
from toolang.execution.types import ControlRef, ThreadPrefix
from toolang.state.prepare import prepare_agent_state


def answer(text):
    return ModelCallResult(message=Message.assistant(text))


def publish(harness, source):
    harness.setup.layout.program.write_text(source)
    harness.published = prepare_agent_state(harness.setup.layout)
    return harness.published


SOURCE = """
agic child() -> Text:
  context = none
  user: Old child.
flow parent() -> Text:
  run child
  run child
  run: Old inline.
"""


def test_publication_updates_named_children_but_retains_inline_plan(tmp_path):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=SOURCE,
        prepare_state=True,
        responses=[
            ScriptedModelTurn(answer("first"), gate=gate),
            answer("second"),
            answer("inline"),
        ],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            latest = publish(harness, "\n\n" + SOURCE.replace("Old", "New"))
            gate.release()
            root = await handle
            assert root.status == "succeeded", (
                harness.store.resolve_error(root.error) if root.error else None
            )
            children = [
                r for r in harness.store.list_run_tree(root_run_id=root.id) if r.parent
            ]
            children.sort(key=lambda r: str(r.parent))
            assert [
                harness.store.resolve_state_revision(r.state) for r in children
            ] == [
                harness.state.revision,
                latest.revision,
                harness.state.revision,
            ]
            for child in children:
                assert child.state == ControlRef.for_run(child.id, 0)
                entry = harness.store.get_run_control(run_id=child.id, index=0)
                assert entry is not None
                assert isinstance(entry.payload, RunControlPayload)
                assert entry.payload.state == harness.store.resolve_state_revision(
                    child.state
                )
                assert all(
                    s.state == child.state
                    for s in harness.store.list_steps(run_id=child.id)
                )
            assert all(
                s.state == root.state for s in harness.store.list_steps(run_id=root.id)
            )
            texts = [
                " ".join(message_text(m.parts) for m in i.call.messages)
                for i in harness.adapter.invocations
            ]
            assert "New child." in texts[1]
            assert "Old inline." in texts[2] and "New inline." not in texts[2]
            assert not any(
                c.kind == "reload"
                for c in harness.store.list_run_controls(run_id=root.id)
            )

    asyncio.run(scenario())


def test_accepted_queued_run_keeps_its_binding(tmp_path):
    source = """
agic parent() -> Text:
  hands = child
  context = none
  Delegate.
agic child() -> Text:
  context = none
  Old child.
"""
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "child",
                        "child",
                        "_toolang__run",
                        {"runnable": "child", "input": {}},
                    ),
                )
            ),
            answer("child"),
            answer("done"),
        ],
    )

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if isinstance(event, StepEnd) and event.kind == "tool":
                tree = harness.store.list_run_tree(root_run_id=event.step.run_id)
                child = next(r for r in tree if r.parent)
                assert child.status == "pending"
                publish(harness, source.replace("Old child", "New child"))

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                ),
                tracer=Tracer(),
            )
            assert root.status == "succeeded", root.error
            child = next(
                r for r in harness.store.list_run_tree(root_run_id=root.id) if r.parent
            )
            assert (
                harness.store.resolve_state_revision(child.state)
                == harness.state.revision
            )
            assert "Old child" in str(harness.adapter.invocations[1].call.messages)

    asyncio.run(scenario())


def test_named_calls_capture_one_publication_each_and_allow_rollback(tmp_path):
    source = SOURCE.replace("  run: Old inline.", "  run child").replace(
        "agic child() -> Text:\n",
        "agic child() -> Text:\n  hands = none\n  handoffs = none\n",
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[answer("first"), answer("second"), answer("third")],
    )
    updated = publish(harness, source.replace("Old child", "New child"))
    revisions = iter(
        tuple(
            state for state in (harness.state, updated, harness.state) for _ in range(3)
        )
    )
    reads = 0

    def latest():
        nonlocal reads
        reads += 1
        return next(revisions)

    harness.executor._state = latest

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                )
            )
            assert root.status == "succeeded", root.error
            assert reads == 9
            children = sorted(
                (
                    r
                    for r in harness.store.list_run_tree(root_run_id=root.id)
                    if r.parent
                ),
                key=lambda r: str(r.parent),
            )
            assert [
                harness.store.resolve_state_revision(r.state) for r in children
            ] == [
                harness.state.revision,
                updated.revision,
                harness.state.revision,
            ]
            assert "New child." in str(harness.adapter.invocations[1].call.messages)
            assert "Old child." in str(harness.adapter.invocations[2].call.messages)

    asyncio.run(scenario())


def test_retry_retains_succeeded_prefix_and_rebinds_failed_suffix(tmp_path):
    source = SOURCE.replace("  run: Old inline.\n", "")
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[
            ScriptedModelTurn(answer("first"), gate=gate),
            RuntimeError("failed attempt"),
        ],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            second = publish(harness, source.replace("Old child", "Second child"))
            gate.release()
            failed = await handle
            assert failed.status == "failed"
            children = [
                r
                for r in harness.store.list_run_tree(root_run_id=failed.id)
                if r.parent
            ]
            succeeded = next(r for r in children if r.status == "succeeded")
            rejected = next(r for r in children if r.status == "failed")
            assert (
                harness.store.resolve_state_revision(rejected.state) == second.revision
            )
            third = publish(harness, source.replace("Old child", "Third child"))
            harness.adapter._responses.append(answer("recovered"))
            root = await harness.executor.retry(
                failed.id,
                setup=harness.setup,
                state=harness.state,
                anchor=harness.store.list_steps(run_id=failed.id)[1].ref,
            )
            assert root.status == "succeeded", root.error
            assert harness.store.get_run(run_id=succeeded.id) == succeeded
            children = [
                r for r in harness.store.list_run_tree(root_run_id=root.id) if r.parent
            ]
            replacement = next(r for r in children if r.id != succeeded.id)
            assert replacement.id != rejected.id
            assert (
                harness.store.resolve_state_revision(replacement.state)
                == third.revision
            )
            assert len(harness.adapter.invocations) == 3

    asyncio.run(scenario())


def test_main_calls_unnamed_export_and_flow_calls_private_helper(tmp_path):
    source = "flow parent():\n  run research\n  run research\n"
    gate = AsyncGate()
    # Compose authored main and external modules through the State preparer.
    home = tmp_path / "agents" / "alice"
    (home / "flows").mkdir(parents=True)
    flow = home / "flows" / "research.too"
    flow.write_text("agic helper():\n  Old helper.\nflow():\n  run helper\n")
    (home / "agent.too").write_text(source)
    from toolang.common.layout import AgentLayout

    state = prepare_agent_state(AgentLayout.resident(tmp_path, "alice"))
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        program=state.modules["agent"],
        state=state,
        responses=[ScriptedModelTurn(answer("first"), gate=gate), answer("second")],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            flow.write_text(
                "\n\nagic helper():\n  New helper.\nflow():\n  run helper\n"
            )
            updated = publish(harness, source)
            gate.release()
            root = await handle
            assert root.status == "succeeded", root.error
            tree = harness.store.list_run_tree(root_run_id=root.id)
            entries = [
                harness.store.get_run_control(run_id=r.id, index=0) for r in tree
            ]
            assert all(e is not None for e in entries)
            helper = [
                e.payload
                for e in entries
                if e is not None
                and isinstance(e.payload, RunControlPayload)
                and e.payload.runnable == "flows::research::agic:helper"
            ]
            assert {e.state for e in helper} == {state.revision, updated.revision}
            assert "New helper" in str(harness.adapter.invocations[1].call.messages)

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["delete", "signature", "missing-source"])
def test_failed_acceptance_has_no_child_and_reports_revisions(tmp_path, change):
    source = SOURCE.replace("  run: Old inline.\n", "")
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[ScriptedModelTurn(answer("first"), gate=gate)],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            candidate = publish(
                harness,
                "flow parent():\n  pass\n"
                if change == "delete"
                else source.replace("child() -> Text", "child() -> Number"),
            )
            if change == "missing-source":
                harness.executor._state = None
            gate.release()
            root = await handle
            assert root.status == "failed"
            children = [
                r for r in harness.store.list_run_tree(root_run_id=root.id) if r.parent
            ]
            assert len(children) == 1 and children[0].status == "succeeded"
            assert root.error is not None
            error = harness.store.resolve_error(root.error)
            if change == "missing-source":
                assert "Published Agent State is unavailable" in error
            else:
                assert "agent::agic:child" in error
                assert harness.state.revision in error and candidate.revision in error

    asyncio.run(scenario())


@pytest.mark.parametrize("routes", ["", "  hands = *\n"])
@pytest.mark.parametrize("incompatible", [False, True])
def test_model_catalog_is_frozen_but_acceptance_selects_latest(
    tmp_path, incompatible, routes
):
    source = f"agic parent() -> Text:\n{routes}  context = none\n  user: Old parent.\n"
    added = source + "flow worker() -> Text:\n  run: Catalog worker.\n"
    gate = AsyncGate()
    call = ModelCallResult(
        tool_calls=(
            ToolCall(
                "run", "run", "_toolang__run", {"runnable": "flow:worker", "input": {}}
            ),
        )
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[
            ScriptedModelTurn(call, gate=gate),
            answer("worked"),
            answer("done"),
        ],
    )
    advertised = publish(harness, added)

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            assert (
                route_snapshots(harness.adapter.invocations[0].call)["hands"][0]["ref"]
                == "flow:worker"
            )
            candidate = publish(
                harness,
                added.replace("Catalog worker", "Accepted worker").replace(
                    "worker() -> Text",
                    "worker() -> Number" if incompatible else "worker() -> Text",
                ),
            )
            gate.release()
            root = await handle
            assert root.status == "succeeded", root.error
            children = [
                r
                for r in harness.store.list_run_tree(root_run_id=root.id)
                if r.parent == harness.store.list_steps(run_id=root.id)[1].ref
            ]
            if incompatible:
                assert not children
                assert "runnable signature changed" in (
                    last_tool_result(harness.adapter.invocations[1].call).error or ""
                )
            else:
                assert len(children) == 1
                assert (
                    harness.store.resolve_state_revision(children[0].state)
                    == candidate.revision
                )
                assert "Accepted worker" in " ".join(
                    message_text(m.parts)
                    for m in harness.adapter.invocations[1].call.messages
                )
            first = harness.store.list_steps(run_id=root.id)[0]
            assert isinstance(first.given, StoredModelStepGiven)
            assert first.given.state == advertised.revision
            assert (
                harness.store.resolve_state_revision(first.state)
                == harness.state.revision
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("routes", ["", "  hands = *\n  handoffs = *\n"])
@pytest.mark.parametrize("operation, mode", [("run", "hands"), ("exec", "handoffs")])
def test_unadvertised_target_waits_for_next_model_catalog(
    tmp_path, routes, operation, mode
):
    source = f"agic parent() -> Text:\n{routes}  context = none\n  Delegate.\n"
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[
            ScriptedModelTurn(
                ModelCallResult(
                    tool_calls=(
                        ToolCall(
                            "run",
                            "run",
                            f"_toolang__{operation}",
                            {"runnable": "worker", "input": {}},
                        ),
                    )
                ),
                gate=gate,
            ),
            answer("done"),
        ],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            assert [
                item["ref"]
                for item in route_snapshots(harness.adapter.invocations[0].call)[mode]
            ] == (["agic:parent"] if mode == "handoffs" else [])
            publish(harness, source + "flow worker():\n  pass\n")
            gate.release()
            root = await handle
            assert root.status == "succeeded", root.error
            assert harness.store.list_run_tree(root_run_id=root.id) == [root]
            assert last_tool_result(harness.adapter.invocations[1].call).error
            assert (
                route_snapshots(harness.adapter.invocations[1].call)[mode][-1]["ref"]
                == "flow:worker"
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("routes", ["", "  hands = *\n"])
def test_tool_batch_reuses_frame_and_next_model_discovers_publication(tmp_path, routes):
    source = f"agic parent() -> Text:\n{routes}  context = none\n  Work.\n"
    skill = tmp_path / "agents/alice/skills/testing/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\ndescription: Test guidance\n---\nBound guidance.\n")
    gate = AsyncGate()
    tool = RecordingTool("test__probe", output={"ok": True})
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        tools={tool.name: tool},
        responses=[
            ScriptedModelTurn(
                ModelCallResult(
                    tool_calls=(
                        ToolCall("probe", "probe", tool.name, {}),
                        ToolCall(
                            "pick",
                            "pick",
                            "_toolang__pick",
                            {"kind": "skill", "ref": "skill/testing"},
                        ),
                    )
                ),
                gate=gate,
            ),
            answer("done"),
        ],
    )
    phase = "model"
    reads = []
    finished_tools = 0

    def latest():
        reads.append(phase)
        return harness.published or harness.state

    harness.executor._state = latest

    class Tracer(RecordingRunTracer):
        async def on_event(self, event):
            nonlocal phase, finished_tools
            await super().on_event(event)
            if isinstance(event, StepEnd):
                if event.kind == "model":
                    phase = "tools"
                elif event.kind == "tool":
                    finished_tools += 1
                    if finished_tools == 2:
                        phase = "model"

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                ),
                tracer=Tracer(),
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            publish(harness, source + "flow worker():\n  pass\n")
            gate.release()
            root = await handle
            assert root.status == "succeeded", root.error
            assert finished_tools == 2 and len(tool.calls) == 1
            assert "tools" not in reads
            assert route_snapshots(harness.adapter.invocations[0].call)["hands"] == []
            assert (
                route_snapshots(harness.adapter.invocations[1].call)["hands"][0]["ref"]
                == "flow:worker"
            )
            assert not last_tool_result(harness.adapter.invocations[1].call).error

    asyncio.run(scenario())


def test_exec_uses_latest_snapshot_and_records_explicit_binding(tmp_path):
    source = (
        "agic parent() -> Text:\n  handoffs = worker\n  context = none\n  Delegate.\n"
    )
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[
            ScriptedModelTurn(
                ModelCallResult(
                    tool_calls=(
                        ToolCall(
                            "execute",
                            "execute",
                            "_toolang__exec",
                            {"runnable": "worker", "input": {}},
                        ),
                    )
                ),
                gate=gate,
            ),
            answer("transferred"),
        ],
    )
    advertised = publish(harness, source + "flow worker():\n  run: Advertised body.\n")

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            latest = publish(harness, source + "flow worker():\n  run: Latest body.\n")
            gate.release()
            root = await handle
            assert root.status == "succeeded", root.error
            transfer = harness.store.list_run_controls(run_id=root.id, kind="exec")[0]
            assert harness.store.resolve_state_revision(transfer.ref) == latest.revision
            steps = harness.store.list_steps(run_id=root.id)
            assert isinstance(steps[0].given, StoredModelStepGiven)
            assert steps[0].given.state == advertised.revision
            assert steps[0].state == ControlRef.for_run(root.id, 0)
            assert steps[-1].state == transfer.ref
            assert "Latest body." in str(harness.adapter.invocations[1].call.messages)

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["run", "exec"])
@pytest.mark.parametrize("selector", ["helper", "*"])
def test_flow_model_routes_are_local_and_keep_advertised_identity(
    tmp_path, operation, selector
):
    from toolang.common.layout import AgentLayout

    home = tmp_path / "agents/alice"
    (home / "flows").mkdir(parents=True)
    source = "agic helper() -> Text:\n  Main helper.\nflow parent() -> Text:\n  run research\n"
    module_source = (
        "agic driver() -> Text:\n"
        f"  {'hands' if operation == 'run' else 'handoffs'} = {selector}\n"
        "  context = none\n  Delegate.\n"
        "agic helper() -> Text:\n  context = none\n  Old local helper.\n"
        "flow() -> Text:\n  run driver\n"
    )
    module = home / "flows/research.too"
    module.write_text(module_source)
    (home / "flows/other.too").write_text("flow:\n  pass\n")
    (home / "agent.too").write_text(source)
    initial = prepare_agent_state(AgentLayout.resident(tmp_path, "alice"))
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        program=initial.modules["agent"],
        state=initial,
        responses=[
            ScriptedModelTurn(
                ModelCallResult(
                    tool_calls=(
                        ToolCall(
                            "delegate",
                            "delegate",
                            f"_toolang__{operation}",
                            {"runnable": "agic:helper"},
                        ),
                    )
                ),
                gate=gate,
            ),
            answer("helper result"),
            *([answer("done")] if operation == "run" else []),
        ],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            module.write_text(module_source.replace("Old local", "New local"))
            publish(harness, source)
            gate.release()
            root = await handle
            assert root.status == "succeeded", root.error
            advertised = route_snapshots(harness.adapter.invocations[0].call)
            enabled = "hands" if operation == "run" else "handoffs"
            assert [item["ref"] for item in advertised[enabled]] == ["agic:helper"]
            helper_call = harness.adapter.invocations[1].call
            if operation == "run":
                assert not last_tool_result(harness.adapter.invocations[2].call).error
            messages = str(helper_call.messages)
            assert "New local helper." in messages
            assert "Main helper." not in messages

    asyncio.run(scenario())


@pytest.mark.parametrize("routes", ["", "  hands = *\n"])
def test_deleted_flow_module_keeps_accepted_agic_and_withdraws_routes(tmp_path, routes):
    from toolang.common.layout import AgentLayout

    home = tmp_path / "agents/alice"
    (home / "flows").mkdir(parents=True)
    source = "agic default:\n  Ready.\n"
    (home / "agent.too").write_text(source)
    module = home / "flows/research.too"
    module.write_text(
        f"agic driver() -> Text:\n{routes}  context = none\n  Bound driver.\n"
        "agic helper() -> Text:\n  Work.\nflow() -> Text:\n  run driver\n"
    )
    initial = prepare_agent_state(AgentLayout.resident(tmp_path, "alice"))
    gate = AsyncGate()
    tool = RecordingTool("test__checkpoint", output={})
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        state=initial,
        tools={tool.name: tool},
        responses=[
            ScriptedModelTurn(
                ModelCallResult(
                    tool_calls=(ToolCall("checkpoint", "checkpoint", tool.name, {}),)
                ),
                gate=gate,
            ),
            answer("done"),
        ],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="research",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            module.unlink()
            publish(harness, source)
            gate.release()
            root = await handle
            assert root.status == "succeeded", root.error
            first, second = [i.call for i in harness.adapter.invocations]
            assert [r["ref"] for r in route_snapshots(first)["hands"]] == [
                "agic:helper"
            ]
            assert route_snapshots(second)["hands"] == []
            assert "Bound driver." in str(second.messages)

    asyncio.run(scenario())


def test_model_catalog_uses_bound_state_without_publication_source(tmp_path):
    source = "agic parent() -> Text:\n  Work.\nflow worker():\n  pass\n"
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[answer("done")],
    )
    harness.executor._state = None

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                )
            )
            assert root.status == "succeeded", root.error
            snapshots = route_snapshots(harness.adapter.invocations[0].call)
            assert [item["ref"] for item in snapshots["hands"]] == ["flow:worker"]
            assert [item["ref"] for item in snapshots["handoffs"]] == [
                "agic:parent",
                "flow:worker",
            ]
            first = harness.store.list_steps(run_id=root.id)[0]
            assert isinstance(first.given, StoredModelStepGiven)
            assert first.given.state == harness.state.revision

    asyncio.run(scenario())


@pytest.mark.parametrize("added_targets", [0, 65])
@pytest.mark.parametrize("routes", ["", "  hands = *\n  handoffs = *\n"])
def test_output_repair_ignores_new_callable_targets(tmp_path, routes, added_targets):
    source = f"agic parent() -> Number:\n{routes}  Return a number.\nflow worker():\n  pass\n"
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[
            ScriptedModelTurn(answer("not a number"), gate=gate),
            answer("42"),
        ],
    )

    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            publish(
                harness,
                source
                + "\n".join(
                    f"flow added_{index}():\n  pass\n" for index in range(added_targets)
                ),
            )
            gate.release()
            root = await handle
            assert root.status == "succeeded", (
                harness.store.resolve_error(root.error) if root.error else None
            )
            assert root.output is not None
            assert harness.store.resolve_value(root.output.value) == 42
            first, repaired = [item.call for item in harness.adapter.invocations]
            assert [item["ref"] for item in route_snapshots(first)["hands"]] == [
                "flow:worker"
            ]
            assert repaired.tools == ()
            assert route_snapshots(
                repaired, requested_only={"hands": False, "handoffs": False}
            ) == {"hands": [], "handoffs": []}

    asyncio.run(scenario())

    assert_replayed(harness.store.db_path, tracer.events)


def test_model_dependencies_refresh_but_prepared_tool_reads_and_code_stay_pinned(
    tmp_path,
):
    source = """
instruct evolution: GENERATION = 1
context progress: Context one.
agic grow() -> Text:
  instruct = evolution
  context = progress
  psyches -= psyche/private
  skills = skill/testing
  hands = none
  handoffs = none
  Original body.
"""
    home = tmp_path / "agents/alice"
    (home / "skills/testing").mkdir(parents=True)
    skill = home / "skills/testing/SKILL.md"
    skill.write_text("---\ndescription: Test skill\n---\nOld skill body.\n")
    (home / "psyches").mkdir()
    (home / "psyches/private.md").write_text("Private advice.")
    (home / "psyches/goal.md").write_text("Initial goal.")

    def pick(identity):
        return ModelCallResult(
            tool_calls=(
                ToolCall(
                    identity,
                    identity,
                    "_toolang__pick",
                    {"kind": "skill", "ref": "skill/testing"},
                ),
            )
        )

    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[
            ScriptedModelTurn(pick("old"), gate=gate),
            pick("new"),
            answer("done"),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="grow",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            skill.write_text("---\ndescription: Test skill\n---\nNew skill body.\n")
            (home / "psyches/goal.md").write_text("Updated goal.")
            (home / "psyches/added.md").write_text("New eligible advice.")
            latest = publish(
                harness,
                source.replace("GENERATION = 1", "GENERATION = 2")
                .replace("Context one.", "Context two.")
                .replace("Original body.", "Changed body.")
                .replace("psyches -= psyche/private", "psyches = none")
                .replace("skills = skill/testing", "skills = none"),
            )
            gate.release()
            root = await handle
            assert root.status == "succeeded", (
                harness.store.resolve_error(root.error) if root.error else None
            )
            first, second, third = [i.call for i in harness.adapter.invocations]
            assert "GENERATION = 1" in first.instructions
            assert "Initial goal." in first.instructions
            assert "New eligible advice." not in first.instructions
            for call in (second, third):
                assert "GENERATION = 2" in call.instructions
                assert "Updated goal." in call.instructions
                assert "New eligible advice." in call.instructions
                assert "Private advice." not in call.instructions
                text = str(call.messages)
                assert "Context two." in text
                assert "Original body." in text and "Changed body." not in text
            from toolang.execution.records import RecallControlPayload
            from toolang.execution.types import SkillRecallTarget

            recalls = [
                c.payload
                for c in harness.store.list_run_controls(run_id=root.id)
                if isinstance(c.payload, RecallControlPayload)
                and isinstance(c.payload.target, SkillRecallTarget)
            ]
            assert [c.content for c in recalls] == [
                "Old skill body.",
                "",
                "New skill body.",
            ]
            models = [
                (s, s.given)
                for s in harness.store.list_steps(run_id=root.id)
                if isinstance(s.given, StoredModelStepGiven)
            ]
            assert [given.state for _, given in models] == [
                harness.state.revision,
                latest.revision,
                latest.revision,
            ]
            assert all(s.state == root.state for s, _ in models)
            assert (
                harness.store.resolve_state_revision(root.state)
                == harness.state.revision
            )

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_ancestor_rules_do_not_exclude_another_modules_private_caps_or_routes(tmp_path):
    source = """
flow root() -> Text:
  psyches = none
  hands = none
  instruct = none
  context = none
  run research
agic helper() -> Text:
  Agent helper must not run.
"""
    home = tmp_path / "agents/alice"
    (home / "flows").mkdir(parents=True)
    (home / "psyches").mkdir()
    (home / "psyches/common.md").write_text("Excluded external advice.")
    module = home / "flows/research.too"
    module.write_text("""
psyche local:
  Module advice.
agic driver() -> Text:
  Delegate to helper.
agic helper() -> Text:
  Local helper body.
flow() -> Text:
  run driver
""")
    from toolang.common.layout import AgentLayout

    (home / "agent.too").write_text(source)
    initial = prepare_agent_state(AgentLayout.resident(tmp_path, "alice"))
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        program=initial.modules["agent"],
        state=initial,
        responses=[
            ScriptedModelTurn(
                ModelCallResult(
                    tool_calls=(
                        ToolCall(
                            "helper", "helper", "_toolang__run", {"runnable": "helper"}
                        ),
                    )
                ),
                gate=gate,
            ),
            answer("helped"),
            answer("done"),
        ],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="root",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            module.write_text(
                module.read_text().replace("Module advice.", "New module advice.")
                + "\npsyche added:\n  New private module advice.\n"
            )
            (home / "psyches/added.md").write_text("New excluded external advice.")
            publish(harness, source)
            gate.release()
            root = await handle
            assert root.status == "succeeded", (
                harness.store.resolve_error(root.error) if root.error else None
            )
            first, helper, resumed = [i.call for i in harness.adapter.invocations]
            assert [r["ref"] for r in route_snapshots(first)["hands"]] == [
                "agic:helper"
            ]
            assert "Module advice." in first.instructions
            assert "New private module advice." not in first.instructions
            for call in (helper, resumed):
                assert "New module advice." in call.instructions
                assert "New private module advice." in call.instructions
                assert "Excluded external advice." not in call.instructions
                assert "New excluded external advice." not in call.instructions
            assert "Local helper body." in str(helper.messages)
            assert "Agent helper must not run." not in str(helper.messages)

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["skill", "service"])
@pytest.mark.parametrize("restriction", ["ancestor", "ceiling"])
def test_new_caps_refresh_under_pinned_rules_and_prepared_picks_stay_frozen(
    tmp_path, kind, restriction
):
    plural = f"{kind}s"
    source = f"""
flow parent() -> Text:
  {plural} -= {kind}/private*
  psyches -= psyche/private*
  run worker
agic worker() -> Text:
  {plural} = *
  psyches = *
  context = none
  hands = none
  handoffs = none
  Original body.
"""
    if restriction == "ceiling":
        source = source.replace(f"  {plural} -= {kind}/private*\n", "")
    home = tmp_path / "agents/alice"

    def write_cap(name):
        path = home / (
            f"skills/{name}/SKILL.md" if kind == "skill" else f"services/{name}.md"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ndescription: {name} guidance\n---\nBody of {name}.\n")
        return path

    removed = write_cap("public_removed")
    write_cap("private_old")
    (home / "psyches").mkdir()
    (home / "psyches/goal.md").write_text("Initial goal.")
    (home / "psyches/private_old.md").write_text("Excluded old psyche.")

    def pick(identity, name):
        return ToolCall(
            identity,
            identity,
            "_toolang__pick",
            {"kind": kind, "ref": f"{kind}/{name}"},
        )

    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        responses=[
            ScriptedModelTurn(
                ModelCallResult(tool_calls=(pick("early", "public_added"),)), gate=gate
            ),
            ModelCallResult(
                tool_calls=(
                    pick("eligible", "public_added"),
                    pick("excluded", "private_added"),
                )
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
                    runnable="parent",
                    ceilings=(AgentCeiling(**{plural: (f"{kind}/public*",)}),)
                    if restriction == "ceiling"
                    else (),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), 2)
            write_cap("public_added")
            write_cap("private_added")
            removed.unlink()
            (home / "psyches/goal.md").write_text("Updated goal.")
            (home / "psyches/added.md").write_text("New eligible psyche.")
            (home / "psyches/private_added.md").write_text("Excluded new psyche.")
            latest = publish(
                harness,
                source.replace(
                    "  run worker", f"  {plural} = none\n  run worker"
                ).replace("Original body.", "Changed body."),
            )
            gate.release()
            root = await asyncio.wait_for(handle, 2)
            assert root.status == "succeeded", (
                harness.store.resolve_error(root.error) if root.error else None
            )
            first, second, third = [i.call for i in harness.adapter.invocations]
            assert f'ref="{kind}/public_removed"' in first.instructions
            assert f'ref="{kind}/public_added"' not in first.instructions
            assert "Initial goal." in first.instructions
            assert last_tool_result(second).error
            for call in (second, third):
                assert f'ref="{kind}/public_added"' in call.instructions
                assert f'ref="{kind}/public_removed"' not in call.instructions
                assert f'ref="{kind}/private_added"' not in call.instructions
                assert f'ref="{kind}/private_old"' not in call.instructions
                assert "Updated goal." in call.instructions
                assert "New eligible psyche." in call.instructions
                assert "Excluded new psyche." not in call.instructions
                assert "Excluded old psyche." not in call.instructions
                assert "Original body." in str(call.messages)
                assert "Changed body." not in str(call.messages)
            assert "Body of public_added." in str(third.messages)
            assert "Body of private_added." not in str(third.messages)
            assert last_tool_result(third).error
            child = next(
                r for r in harness.store.list_run_tree(root_run_id=root.id) if r.parent
            )
            steps = harness.store.list_steps(run_id=child.id)
            assert [s.status for s in steps if s.kind == "tool"] == [
                "failed",
                "succeeded",
                "failed",
            ]
            models = [
                (s, s.given) for s in steps if isinstance(s.given, StoredModelStepGiven)
            ]
            assert [given.state for _, given in models] == [
                harness.state.revision,
                latest.revision,
                latest.revision,
            ]
            assert all(s.state == child.state for s, _ in models)

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)


def test_external_tool_ceiling_does_not_require_children_to_restore_excluded_tools(
    tmp_path,
):
    from toolang.base.types.policy import AgentCeiling

    tool = RecordingTool("test__probe", output={})
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow parent() -> Text:
  tools = none
  run worker
agic worker() -> Text:
  tools = *
  Work without user tools.
""",
        tools={tool.name: tool},
        responses=[answer("done")],
    )

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="parent",
                    ceilings=(AgentCeiling(tools=("test/*",)),),
                )
            )
            assert root.status == "succeeded", (
                harness.store.resolve_error(root.error) if root.error else None
            )
            assert tool.name not in {
                t.name for t in harness.adapter.invocations[0].call.tools
            }

    asyncio.run(scenario())
