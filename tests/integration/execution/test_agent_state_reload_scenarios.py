"""Agent State reload boundaries across one active run tree."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
import json
from pathlib import Path
import threading

import pytest

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult
from toolang.common.ids import IdIssuer
from toolang.execution.executor import RunExecutor
from toolang.execution.executor.common import BoundRun, Local
from toolang.execution.executor.executor import _Execution
from toolang.execution.records import RunControlPayload, ControlRecord
from toolang.execution.types import (
    ControlRef,
    ControlTiming,
    Occurrence,
    StepRef,
    ThreadPrefix,
)
from toolang.state.cache import agent_revision_dir
from toolang.state.prepare import prepare_agent_state
from toolang.state.state import AgentState


_ROOT_SOURCE = """
agic child:
  recall = none
  context: none
  instruct: old state
  user: hello

flow parent:
  run child
  run child
""".lstrip()

_RELOADED_SOURCE = _ROOT_SOURCE.replace("old state", "new state")
_RELOADED_TWICE_SOURCE = _ROOT_SOURCE.replace("old state", "newest state")

_ACTIVE_AGIC_SOURCE = """
instruct:
  old state

agic active -> Number:
  recall = none
  context: none
  user: hello
""".lstrip()

_RELOADED_ACTIVE_AGIC_SOURCE = _ACTIVE_AGIC_SOURCE.replace(
    "old state",
    "new state",
).replace("-> Number", "-> Boolean")

_PARALLEL_SOURCE = """
instruct:
  old state

agic child(_: Part[]) -> Part[]:
  recall = none
  context: none
  user: hello

flow parent(_: Part[]) -> Part[][]:
  storm 2 using child in 2 lanes
""".lstrip()

_RELOADED_PARALLEL_SOURCE = _PARALLEL_SOURCE.replace("old state", "new state")


def _durable_state(harness: ExecutionHarness, source: str) -> AgentState:
    layout = harness.setup.layout
    layout.home.mkdir(parents=True, exist_ok=True)
    layout.program.write_text(source, encoding="utf-8")
    return prepare_agent_state(layout)


async def _wait_until_applied(
    harness: ExecutionHarness,
    run_id: str,
    index: int,
) -> None:
    async def wait() -> None:
        while True:
            control = harness.store.get_run_control(run_id=run_id, index=index)
            if control is not None and control.status == "applied":
                return
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=1)


def test_reload_orders_step_state_and_child_acceptance_at_one_boundary(
    tmp_path: Path,
) -> None:
    first_call = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=_ROOT_SOURCE,
        responses=(
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("first")),
                gate=first_call,
            ),
            ModelCallResult(message=Message.assistant("second")),
        ),
    )
    reloaded = _durable_state(harness, _RELOADED_SOURCE)

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="flow:parent",
                    primary=Message.user("start").parts,
                )
            )
            await first_call.wait_until_entered()
            first_child = next(
                run
                for run in harness.store.list_runs(thread_id=thread, limit=None)
                if run.parent is not None
            )
            control = harness.executor.reload(
                run_id=first_child.id,
                state=reloaded,
                request_id="reload-state",
            )
            active = harness.executor._active[handle.run_id]
            assert str(control.target) == handle.run_id
            await _wait_until_applied(harness, handle.run_id, control.index)
            assert control.index not in active.reload_states
            first_call.release()
            root = await handle

            assert root.status == "succeeded", root.error
            assert root.state == ControlRef.for_run(root.id, 0)
            assert (
                harness.store.resolve_state_revision(root.state)
                == harness.state.revision
            )
            assert (
                harness.store.resolve_state_revision(
                    ControlRef.for_run(root.id, control.index)
                )
                == reloaded.revision
            )

            parent_steps = [
                step
                for step in harness.store.list_steps(run_id=root.id)
                if step.kind == "run"
            ]
            assert [step.state for step in parent_steps] == [
                ControlRef.for_run(root.id, 0),
                ControlRef.for_run(root.id, control.index),
            ]
            children = [
                run
                for run in harness.store.list_runs(thread_id=thread, limit=None)
                if run.parent is not None
            ]
            children_by_parent = {child.parent: child for child in children}
            assert [children_by_parent[step.ref].state for step in parent_steps] == [
                step.state for step in parent_steps
            ]
            for child in children:
                entry = harness.store.get_run_control(run_id=child.id, index=0)
                assert entry is not None
                assert isinstance(entry.payload, RunControlPayload)
                assert entry.payload.state is None
            assert "old state" in harness.adapter.invocations[0].call.instructions
            assert "new state" in harness.adapter.invocations[1].call.instructions

    asyncio.run(scenario())


def test_concurrent_reloads_apply_in_control_index_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_call = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=_ROOT_SOURCE,
        responses=(
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("first")),
                gate=first_call,
            ),
            ModelCallResult(message=Message.assistant("second")),
        ),
    )
    first_state = _durable_state(harness, _RELOADED_SOURCE)
    second_state = _durable_state(harness, _RELOADED_TWICE_SOURCE)
    original_accept = harness.store.accept_reload_control
    first_accepted = threading.Event()
    release_first = threading.Event()
    second_accepted = threading.Event()

    def delayed_accept(
        *,
        run_id: str,
        state: str,
        timing: ControlTiming = "immediate",
        request_id: str | None,
        created_at: str,
        triggered_by: StepRef | None = None,
    ) -> ControlRecord:
        control = original_accept(
            run_id=run_id,
            state=state,
            timing=timing,
            request_id=request_id,
            created_at=created_at,
            triggered_by=triggered_by,
        )
        if request_id == "reload-first":
            first_accepted.set()
            if not release_first.wait(timeout=2):
                raise AssertionError("timed out waiting to release the first reload")
        elif request_id == "reload-second":
            second_accepted.set()
        return control

    monkeypatch.setattr(harness.store, "accept_reload_control", delayed_accept)

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="flow:parent",
                    primary=Message.user("start").parts,
                )
            )
            await first_call.wait_until_entered()
            first_reload = asyncio.create_task(
                asyncio.to_thread(
                    handle.reload,
                    first_state,
                    request_id="reload-first",
                )
            )
            assert await asyncio.to_thread(first_accepted.wait, 1)
            second_reload = asyncio.create_task(
                asyncio.to_thread(
                    handle.reload,
                    second_state,
                    request_id="reload-second",
                )
            )
            accepted_out_of_order = await asyncio.to_thread(
                second_accepted.wait,
                0.1,
            )
            release_first.set()
            first_control, second_control = await asyncio.gather(
                first_reload,
                second_reload,
            )

            assert not accepted_out_of_order
            assert [first_control.index, second_control.index] == [1, 2]
            await _wait_until_applied(
                harness,
                handle.run_id,
                first_control.index,
            )
            await _wait_until_applied(
                harness,
                handle.run_id,
                second_control.index,
            )
            active = harness.executor._active[handle.run_id]
            assert active.reload_states == {}
            first_call.release()
            root = await handle

            assert root.status == "succeeded", root.error
            assert "newest state" in harness.adapter.invocations[1].call.instructions

    asyncio.run(scenario())


def test_reload_refreshes_the_next_step_of_an_active_agic(tmp_path: Path) -> None:
    first_call = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=_ACTIVE_AGIC_SOURCE,
        responses=(
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("1")),
                gate=first_call,
            ),
            ModelCallResult(message=Message.assistant("7")),
        ),
    )
    reloaded = _durable_state(harness, _RELOADED_ACTIVE_AGIC_SOURCE)

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="agic:active",
                    primary=Message.user("start").parts,
                )
            )
            await first_call.wait_until_entered()
            control = handle.reload(reloaded, request_id="reload-active-agic")
            await _wait_until_applied(harness, handle.run_id, control.index)
            handle.steer(Message.user("continue"), timing="next_call")
            first_call.release()
            root = await handle

            assert root.status == "succeeded", root.error
            steps = [
                step
                for step in harness.store.list_steps(run_id=root.id)
                if step.kind == "model"
            ]
            assert [step.state for step in steps] == [
                ControlRef.for_run(root.id, 0),
                ControlRef.for_run(root.id, control.index),
            ]
            assert "old state" in harness.adapter.invocations[0].call.instructions
            assert "new state" in harness.adapter.invocations[1].call.instructions
            assert harness.adapter.invocations[0].call.output_schema == {
                "type": "number"
            }
            assert harness.adapter.invocations[1].call.output_schema == {
                "type": "number"
            }
            assert root.output is not None
            assert harness.store.resolve_value(root.output.local.value) == 7

    asyncio.run(scenario())


def test_parallel_steps_record_the_state_on_their_boundary_side(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_call = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=_PARALLEL_SOURCE,
        responses=(
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("first")),
                gate=first_call,
            ),
            ModelCallResult(message=Message.assistant("second")),
        ),
    )
    reloaded = _durable_state(harness, _RELOADED_PARALLEL_SOURCE)
    original_execute_child = _Execution.execute_child

    async def scenario() -> None:
        second_child_waiting = asyncio.Event()
        allow_second_child = asyncio.Event()
        started_children = 0

        async def gate_second_child(
            execution: _Execution,
            parent: BoundRun,
            locals: Mapping[str, Local],
            step: StepRef,
            name: str,
            occurrence: Occurrence | None,
            *,
            output_binding: str | None = "_",
            expected_output: str | None = None,
        ) -> Local:
            nonlocal started_children
            started_children += 1
            if started_children == 2:
                second_child_waiting.set()
                await allow_second_child.wait()
            return await original_execute_child(
                execution,
                parent,
                locals,
                step,
                name,
                occurrence,
                output_binding=output_binding,
                expected_output=expected_output,
            )

        monkeypatch.setattr(_Execution, "execute_child", gate_second_child)
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="flow:parent",
                    primary=Message.user("start").parts,
                )
            )
            await asyncio.wait_for(
                asyncio.gather(
                    first_call.wait_until_entered(),
                    second_child_waiting.wait(),
                ),
                timeout=1,
            )
            control = handle.reload(reloaded, request_id="reload-parallel")
            await _wait_until_applied(harness, handle.run_id, control.index)
            allow_second_child.set()

            async def wait_for_second_call() -> None:
                while len(harness.adapter.invocations) < 2:
                    if handle.task.done():
                        root = await handle
                        raise AssertionError(root.error)
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_for_second_call(), timeout=1)
            assert first_call.entered
            first_call.release()
            root = await handle

            assert root.status == "succeeded", root.error
            children = [
                run
                for run in harness.store.list_runs(thread_id=thread, limit=None)
                if run.parent is not None
            ]
            assert len(children) == 2
            assert {child.state for child in children} == {
                ControlRef.for_run(root.id, 0),
                ControlRef.for_run(root.id, control.index),
            }
            child_steps = [
                step
                for child in children
                for step in harness.store.list_steps(run_id=child.id)
                if step.kind == "model"
            ]
            calls = harness.store.rebuild_model_calls(child_steps)
            by_instruction = {
                calls[step.ref]
                .instructions.partition("<toolang:instruct>\n")[2]
                .partition("\n</toolang:instruct>")[0]: step.state
                for step in child_steps
            }
            assert by_instruction == {
                "old state": ControlRef.for_run(root.id, 0),
                "new state": ControlRef.for_run(root.id, control.index),
            }

    asyncio.run(scenario())


def test_reload_rejects_non_durable_and_cross_layout_state(tmp_path: Path) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=_ROOT_SOURCE,
        responses=(
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("first")),
                gate=gate,
            ),
            ModelCallResult(message=Message.assistant("second")),
        ),
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="flow:parent",
                    primary=Message.user("start").parts,
                )
            )
            await gate.wait_until_entered()
            with pytest.raises(ValueError, match="durable"):
                harness.executor.reload(run_id=handle.run_id, state=harness.state)

            empty_revision_dir = agent_revision_dir(
                harness.setup.layout,
                harness.state.revision,
            )
            empty_revision_dir.mkdir(parents=True)
            empty_state = replace(
                harness.state,
                revision_dir=empty_revision_dir,
            )
            with pytest.raises(ValueError, match="durable"):
                harness.executor.reload(run_id=handle.run_id, state=empty_state)

            cross_layout = _durable_state(harness, _RELOADED_SOURCE)
            forged = replace(cross_layout, config={"forged": True})
            with pytest.raises(ValueError, match="does not match"):
                harness.executor.reload(run_id=handle.run_id, state=forged)

            assert cross_layout.revision_dir is not None
            foreign_dir = tmp_path / "foreign" / cross_layout.revision
            foreign_dir.mkdir(parents=True)
            foreign = AgentState(
                name=cross_layout.name,
                allow_overrides=cross_layout.allow_overrides,
                revision=cross_layout.revision,
                root_revision=cross_layout.root_revision,
                home_revision=cross_layout.home_revision,
                root_config=cross_layout.root_config,
                home_config=cross_layout.home_config,
                config=cross_layout.config,
                caps=cross_layout.caps,
                modules=cross_layout.modules,
                module_sources=cross_layout.module_sources,
                module_digests=cross_layout.module_digests,
                module_caps=cross_layout.module_caps,
                revision_dir=foreign_dir,
            )
            with pytest.raises(ValueError, match="another layout"):
                harness.executor.reload(run_id=handle.run_id, state=foreign)
            remote = RunExecutor(
                harness.store,
                IdIssuer(tmp_path / "remote-ids.json"),
            )
            with pytest.raises(ValueError, match="not owned"):
                remote.reload(run_id=handle.run_id, state=cross_layout)
            await remote.stop()
            gate.release()
            await handle
            with pytest.raises(ValueError, match="not owned"):
                harness.executor.reload(run_id=handle.run_id, state=cross_layout)

    asyncio.run(scenario())


def test_revoked_reload_releases_its_retained_state(tmp_path: Path) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=_ROOT_SOURCE,
        responses=(
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("first")),
                gate=gate,
            ),
            ModelCallResult(message=Message.assistant("second")),
        ),
    )
    reloaded = _durable_state(harness, _RELOADED_SOURCE)

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="flow:parent",
                    primary=Message.user("start").parts,
                )
            )
            await gate.wait_until_entered()
            active = harness.executor._active[handle.run_id]
            control = handle.reload(reloaded, request_id="reload-revoked")
            assert active.reload_states[control.index] is reloaded

            revoked = handle.cancel_control(control.index)

            assert revoked.status == "revoked"
            assert control.index not in active.reload_states
            gate.release()
            root = await handle
            assert root.status == "succeeded", root.error
            assert all(
                "old state" in invocation.call.instructions
                for invocation in harness.adapter.invocations
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("layer", ["message", "context"])
@pytest.mark.parametrize("initial_depth,updated_depth", [(0, 2), (2, 0)])
def test_until_reload_uses_the_current_condition_history_requirement(
    tmp_path: Path, layer: str, initial_depth: int, updated_depth: int
) -> None:
    def condition(depth: int) -> str:
        return f"Compare {{{{_{depth}._}}}}." if depth else "Return true."

    source = f"""
context:
  {condition(initial_depth) if layer == "context" else "Condition context."}
agic worker:
  instruct: none
  context: none
  user: Work.
flow parent:
  repeat 3 times:
    run worker
    until: {condition(initial_depth) if layer == "message" else "Return true."}
"""
    replacement = source.replace(condition(initial_depth), condition(updated_depth), 1)
    first_call = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=(
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("round 1")),
                gate=first_call,
            ),
            *(
                ModelCallResult(message=Message.assistant(f"round {index + 2}"))
                for index in range(updated_depth)
            ),
            ModelCallResult(message=Message.assistant("true")),
        ),
    )
    reloaded = _durable_state(harness, replacement)

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="flow:parent",
                    primary=Message.user("seed").parts,
                )
            )
            await first_call.wait_until_entered()
            control = handle.reload(reloaded)
            await _wait_until_applied(harness, handle.run_id, control.index)
            first_call.release()
            root = await handle
            assert root.status == "succeeded", (
                harness.store.resolve_error(root.error) if root.error else None
            )
            assert len(harness.adapter.invocations) == updated_depth + 2
            assert harness.store.run_output_text(run_id=root.id) == (
                f"round {updated_depth + 1}"
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["map", "storm", "settle"])
@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize("output", ["Text", "Text[]"])
@pytest.mark.parametrize("changes_type", [True, False])
def test_collection_reload_preserves_the_operation_output_contract(
    tmp_path: Path, operation: str, kind: str, output: str, changes_type: bool
) -> None:
    updated_output = output.replace("Text", "Number") if changes_type else output
    source_values = (
        [[value] for value in "abc"] if output.endswith("[]") else list("abc")
    )
    results = (
        [[value] for value in ("first", "3", "4")]
        if output.endswith("[]")
        else ["first", "3", "4"]
    )
    responses = [
        json.dumps(value) if isinstance(value, list) else value for value in results
    ]
    worker = "transform" if kind == "agic" else "worker"
    declaration = (
        "" if kind == "agic" else f"flow transform -> {output}:\n  run worker\n"
    )
    statement = {
        "map": "map in 1 lane using transform",
        "storm": "storm 3 in 1 lane using transform",
        "settle": "settle using transform",
    }[operation]
    source = f"""
agic seed() -> {output}[]:
  Seed.
agic {worker} -> {output}:
  user: Old current {{{{_}}}}.
{declaration}
flow parent() -> {output if operation == "settle" else f"{output}[]"}:
  scatter 3 using seed
  {statement}
"""
    replacement = (
        source.replace("Old current", "New current")
        .replace(f"transform -> {output}:", f"transform -> {updated_output}:")
        .replace(f"worker -> {output}:", f"worker -> {updated_output}:")
    )
    first_call = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=(
            ModelCallResult(message=Message.assistant(json.dumps(source_values))),
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant(responses[0])),
                gate=first_call,
            ),
            ModelCallResult(message=Message.assistant(responses[1])),
            ModelCallResult(message=Message.assistant(responses[2])),
        ),
    )
    reloaded = _durable_state(harness, replacement)

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(thread=thread, runnable="flow:parent")
            )
            await asyncio.wait_for(first_call.wait_until_entered(), timeout=1)
            control = handle.reload(reloaded)
            await _wait_until_applied(harness, handle.run_id, control.index)
            first_call.release()
            root = await handle

            if changes_type:
                assert root.status == "failed"
                assert len(harness.adapter.invocations) == 2
                assert root.error is not None
                assert f"requires {output} output" in harness.store.resolve_error(
                    root.error
                )
                step = harness.store.list_steps(run_id=root.id)[-1]
                assert step.status == "failed" and step.output is None
                children = [
                    run
                    for run in harness.store.list_runs(thread_id=thread, limit=None)
                    if run.parent == step.ref
                ]
                assert len(children) == 1
            else:
                assert root.status == "succeeded", root.error
                assert len(harness.adapter.invocations) == (
                    3 if operation == "settle" else 4
                )
                assert "New current" in str(
                    harness.adapter.invocations[2].call.messages
                )
                expected = results[1] if operation == "settle" else results
                assert harness.store.run_output_text(run_id=root.id) == (
                    json.dumps(expected, separators=(",", ":"))
                    if isinstance(expected, list)
                    else expected
                )

            # Failed operations must not leave an unreadable typed output behind.
            for run in harness.store.list_runs(thread_id=thread, limit=None):
                if run.output is not None:
                    harness.store.run_output_text(run_id=run.id)

    asyncio.run(scenario())
