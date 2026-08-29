"""Agent State reload boundaries across one active run tree."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
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
    StepPath,
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

agic active:
  recall = none
  context: none
  user: hello
""".lstrip()

_RELOADED_ACTIVE_AGIC_SOURCE = _ACTIVE_AGIC_SOURCE.replace(
    "old state",
    "new state",
)

_PARALLEL_SOURCE = """
instruct:
  old state

agic child(_: Part[]) -> Part[]:
  recall = none
  context: none
  user: hello

flow parent(_: Part[]):
  storm 2 child par 2
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
            assert control.target == handle.run_id
            await _wait_until_applied(harness, handle.run_id, control.index)
            assert control.index not in active.reload_states
            first_call.release()
            root = await handle

            assert root.status == "succeeded", root.error
            assert root.state == ControlRef(root.id, 0)
            assert (
                harness.store.resolve_state_revision(root.state)
                == harness.state.revision
            )
            assert (
                harness.store.resolve_state_revision(ControlRef(root.id, control.index))
                == reloaded.revision
            )

            parent_steps = [
                step
                for step in harness.store.list_steps(run_id=root.id)
                if step.kind == "run"
            ]
            assert [step.state for step in parent_steps] == [
                ControlRef(root.id, 0),
                ControlRef(root.id, control.index),
            ]
            children = [
                run
                for run in harness.store.list_runs(thread_id=thread, limit=None)
                if run.parent is not None
            ]
            children_by_parent = {child.parent: child for child in children}
            assert [children_by_parent[step.path].state for step in parent_steps] == [
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
    ) -> ControlRecord:
        control = original_accept(
            run_id=run_id,
            state=state,
            timing=timing,
            request_id=request_id,
            created_at=created_at,
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
                result=ModelCallResult(message=Message.assistant("first")),
                gate=first_call,
            ),
            ModelCallResult(message=Message.assistant("second")),
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
                ControlRef(root.id, 0),
                ControlRef(root.id, control.index),
            ]
            assert "old state" in harness.adapter.invocations[0].call.instructions
            assert "new state" in harness.adapter.invocations[1].call.instructions

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
            step: StepPath,
            name: str,
            occurrence: Occurrence | None,
            *,
            output_name: str | None = "_",
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
                output_name=output_name,
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
                ControlRef(root.id, 0),
                ControlRef(root.id, control.index),
            }
            child_steps = [
                step
                for child in children
                for step in harness.store.list_steps(run_id=child.id)
                if step.kind == "model"
            ]
            calls = harness.store.rebuild_model_calls(child_steps)
            by_instruction = {
                calls[step.path].instructions.partition(
                    "\n\nThe inner runtime tools are available"
                )[0]: step.state
                for step in child_steps
            }
            assert by_instruction == {
                "old state": ControlRef(root.id, 0),
                "new state": ControlRef(root.id, control.index),
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
