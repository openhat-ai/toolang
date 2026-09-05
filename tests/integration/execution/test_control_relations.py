"""Durable control identities and Step boundary relations."""

from pathlib import Path
import sqlite3
import asyncio

import pytest
from pydantic import TypeAdapter

from tests.support.execution_fixtures import (
    project_run_start,
    project_run_end,
    project_step,
)
from tests.support.execution_harness import (
    ExecutionHarness,
    AsyncGate,
    ScriptedModelTurn,
    RecordingRunTracer,
)
from tests.support.execution_assertions import assert_run_event_integrity
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.events import RunEnd
from toolang.execution.types import ThreadPrefix
from toolang.base.types.message import Message, ToolResultPart
from toolang.execution.records import (
    ControlRecord,
    RecallControlPayload,
    RunControlPayload,
    control_payload_from_data,
    control_payload_to_data,
)
from toolang.execution.schemas import record_to_data
from toolang.execution.store import RunStore
from toolang.execution.types import (
    RulesRecallTarget,
    SkillRecallTarget,
    ServiceRecallTarget,
    ControlRef,
    Local,
    ModelStepGiven,
    StepRef,
)
from toolang.base.types.run import ModelCall
from toolang.lang.input import resolve_input_parts


@pytest.mark.parametrize("action", ["run", "execute"])
def test_child_rejects_its_own_runnable(tmp_path: Path, action: str) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow outer() -> Text:
  run inner

agic inner() -> Text:
  recall = none
  hands = agic:inner
  handoffs = agic:inner
  user: Inner.
""",
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "self", "self", f"_too__{action}", {"runnable": "agic:inner"}
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("recovered")),
            ModelCallResult(message=Message.assistant("unexpected recursion")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="flow:outer",
                )
            )
            assert root.status == "succeeded", root.error
            assert len(harness.store.list_run_tree(root_run_id=root.id)) == 2
            assert len(harness.adapter.invocations) == 2
            result = harness.adapter.invocations[1].call.messages[-1].parts[0]
            assert isinstance(result, ToolResultPart)
            assert result.error == (
                f"_too/{action} cannot call the current or an ancestor runnable: agic:inner"
            )

    asyncio.run(scenario())


def test_child_control_relations_preserve_their_owner(tmp_path: Path) -> None:
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="flow outer() -> Text:\n  run inner\n\nagic inner() -> Text:\n  user: Inner.\n",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("unused")), gate=gate
            )
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="flow:outer",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            root, child = harness.store.list_run_tree(root_run_id=handle.run_id)
            steer = harness.executor.steer(
                run_id=child.id,
                message=Message.user("not consumed"),
                timing="next_call",
            )
            cancel = handle.cancel(reason="cancel root")
            await asyncio.wait_for(handle, timeout=2)
            assert steer.index == cancel.index
            saved_steer = harness.store.get_run_control(
                run_id=child.id, index=steer.index
            )
            assert saved_steer is not None and saved_steer.status == "wontapply"
            for run in (root, child):
                first = harness.store.list_steps(run_id=run.id)[0]
                assert first.preceded_by == (ControlRef.for_run(run.id, 0),)
                assert first.aborted_by == cancel.ref
            ends = [event for event in tracer.events if isinstance(event, RunEnd)]
            assert [event.control for event in ends] == [cancel.ref, cancel.ref]

    asyncio.run(scenario())


def test_parallel_children_keep_separate_consumption_relations_after_restart(
    tmp_path: Path,
) -> None:
    gates = [AsyncGate() for _ in range(3)]
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
flow outer(_: Part[]):
  storm 2 inner par 2

agic inner(_: Part[]) -> Text:
  recall = none
  user: Inner.
""",
        responses=[
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("draft")), gate=gate
            )
            for gate in gates
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="outer",
                    primary=resolve_input_parts("work"),
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(
                asyncio.gather(*(gate.wait_until_entered() for gate in gates[:2])),
                timeout=1,
            )
            children = [
                run
                for run in harness.store.list_run_tree(root_run_id=handle.run_id)
                if run.parent is not None
            ]
            assert len(children) == 2
            steers = {
                child.id: harness.executor.steer(
                    run_id=child.id,
                    message=Message.user(f"Revise {child.id}"),
                    timing="next_call",
                )
                for child in children
            }
            gates[0].release()
            await asyncio.wait_for(gates[2].wait_until_entered(), timeout=1)
            # Identify the consuming branch by its records, not sibling order.
            consumer = next(
                child.id
                for child in children
                if len(harness.store.list_steps(run_id=child.id)) == 2
            )
            pending = next(child.id for child in children if child.id != consumer)
            cancel = handle.cancel(reason="stop parallel work")
            root = await asyncio.wait_for(handle, timeout=2)
            assert root.status == "canceled"
            assert {item.index for item in steers.values()} == {cancel.index}
            runs = harness.store.list_run_tree(root_run_id=root.id)
            snapshots = {
                run.id: (
                    harness.store.list_steps(run_id=run.id),
                    harness.store.list_run_controls(run_id=run.id),
                )
                for run in runs
            }
            for child in children:
                first = snapshots[child.id][0][0]
                assert first.preceded_by == (ControlRef.for_run(child.id, 0),)
                assert first.aborted_by == (
                    None if child.id == consumer else cancel.ref
                )
            first, second = snapshots[consumer][0]
            assert first.status == "succeeded"
            assert second.preceded_by == (steers[consumer].ref,)
            assert second.aborted_by == cancel.ref
            assert second.status == "canceled"
            assert snapshots[consumer][1][-1].status == "applied"
            assert snapshots[pending][1][-1].status == "wontapply"
            assert all(run.status == "canceled" for run in runs)
            assert_run_event_integrity(tracer.events)
        reopened = RunStore(harness.store.db_path)
        try:
            for run_id, (steps, controls) in snapshots.items():
                assert reopened.list_steps(run_id=run_id) == steps
                assert reopened.list_run_controls(run_id=run_id) == controls
                for step in steps:
                    refs = (
                        *step.preceded_by,
                        *((step.aborted_by,) if step.aborted_by else ()),
                    )
                    assert steers[pending].ref not in refs
                    for ref in refs:
                        control = reopened.get_run_control(
                            run_id=str(ref.target), index=ref.index
                        )
                        assert control is not None and control.status == "applied"
        finally:
            reopened.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "target",
    [
        RulesRecallTarget("project", "/src"),
        SkillRecallTarget("python-testing"),
        ServiceRecallTarget("github"),
    ],
)
def test_recall_preserves_target_revision_and_raw_content(
    tmp_path: Path, target
) -> None:
    payload = RecallControlPayload(
        target, "revision-1", "Rules and guidance <stay> unwrapped.\n"
    )
    assert (
        control_payload_from_data("recall", control_payload_to_data(payload)) == payload
    )
    store = RunStore(tmp_path / "runs.db")
    run = project_run_start(
        store,
        run_id="run_recall",
        thread_id="term_recall",
        origin="test",
        input=Message.user("hello"),
    )
    trigger = project_step(
        store,
        run_id=run.id,
        step_index=0,
        kind="tool",
        status="succeeded",
        input=(),
        output=(),
        started_at="2026-01-01T00:00:01Z",
        finished_at="2026-01-01T00:00:02Z",
    )
    control = store.accept_recall_control(
        run_id=run.id,
        payload=payload,
        triggered_by=trigger.ref,
        created_at="2026-01-01T00:00:02Z",
    )
    updated = store.accept_recall_control(
        run_id=run.id,
        payload=RecallControlPayload(target, "revision-2", "Updated"),
        triggered_by=trigger.ref,
        created_at="2026-01-01T00:00:03Z",
    )
    step = store.begin_step(
        ref=StepRef.from_local(run.id, (1,)),
        kind="model",
        input=(),
        preceded_by=(control.ref, updated.ref),
        given=ModelStepGiven("test", ModelCall(instructions="", messages=[])),
        started_at="2026-01-01T00:00:04Z",
    )
    assert control.status == updated.status == "applied"
    assert control.payload == payload
    assert (
        TypeAdapter(ControlRecord).validate_python(record_to_data(control)) == control
    )
    store.close()
    reopened = RunStore(tmp_path / "runs.db")
    try:
        assert reopened.get_run_control(run_id=run.id, index=control.index) == control
        assert reopened.get_step(ref=step.ref) == step
    finally:
        reopened.close()


@pytest.mark.parametrize("reject_cleanup", [False, True])
def test_retry_removes_emitted_controls_without_reusing_indexes(
    tmp_path: Path, reject_cleanup: bool
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_retry",
            thread_id="term_retry",
            origin="test",
            input=Message.user("hello"),
            runnable_kind="flow",
        )
        prefix = project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="value",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        trigger = project_step(
            store,
            run_id=run.id,
            step_index=1,
            kind="tool",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-01-01T00:00:03Z",
            finished_at="2026-01-01T00:00:04Z",
        )
        pending = store.accept_run_control(
            run_id=run.id,
            kind="cancel",
            timing="next_step",
            locals=(Local.typed("Text", "stop", "_"),),
            request_id="cancel-request",
            created_at="2026-01-01T00:00:04Z",
        )
        recall = store.accept_recall_control(
            run_id=run.id,
            payload=RecallControlPayload(
                SkillRecallTarget("testing"), "v1", "Use tests."
            ),
            triggered_by=trigger.ref,
            created_at="2026-01-01T00:00:05Z",
        )
        project_run_end(store, run_id=run.id, status="failed")
        entry = store.get_run_control(run_id=run.id, index=0)
        assert entry is not None and isinstance(entry.payload, RunControlPayload)
        revision = store.latest_run_control_revision()
        if reject_cleanup:
            with sqlite3.connect(store.db_path) as connection:
                connection.execute("""CREATE TRIGGER reject_cleanup BEFORE DELETE ON controls
                    WHEN OLD.kind = 'recall' BEGIN SELECT RAISE(ABORT, 'cleanup failure'); END""")
            before_run = store.get_run(run_id=run.id)
            before_controls = store.list_run_controls(run_id=run.id)
            with pytest.raises(ValueError):
                store.accept_retry(
                    run_id=run.id,
                    anchor=trigger.ref,
                    resources=entry.payload.resources,
                    limits=entry.payload.limits,
                    state=entry.payload.state,
                    model_request=entry.payload.model_request,
                    sandbox="host",
                    request_id="retry-request",
                    created_at="2026-01-01T00:00:06Z",
                )
            assert store.list_steps(run_id=run.id) == [prefix, trigger]
            assert store.list_run_controls(run_id=run.id) == before_controls
            assert store.get_run(run_id=run.id) == before_run
            assert store.latest_run_control_revision() == revision
            with sqlite3.connect(store.db_path) as connection:
                connection.execute("DROP TRIGGER reject_cleanup")
        _, retry, _ = store.accept_retry(
            run_id=run.id,
            anchor=trigger.ref,
            resources=entry.payload.resources,
            limits=entry.payload.limits,
            state=entry.payload.state,
            model_request=entry.payload.model_request,
            sandbox="host",
            request_id="retry-request",
            created_at="2026-01-01T00:00:06Z",
        )
        assert store.list_steps(run_id=run.id) == [prefix]
        assert store.get_run_control(run_id=run.id, index=recall.index) is None
        failed_pending = store.get_run_control(run_id=run.id, index=pending.index)
        assert failed_pending is not None and failed_pending.status == "wontapply"
        assert retry.index == recall.index + 1
        _, changes = store.changed_run_controls(after_revision=revision)
        assert {item.ref for item in changes} == {retry.ref, pending.ref}
        replacement = project_step(
            store,
            run_id=run.id,
            step_index=1,
            kind="tool",
            status="succeeded",
            input=(),
            output=(),
            started_at="2026-01-01T00:00:07Z",
            finished_at="2026-01-01T00:00:08Z",
        )
        assert isinstance(recall.payload, RecallControlPayload)
        new_recall = store.accept_recall_control(
            run_id=run.id,
            payload=recall.payload,
            triggered_by=replacement.ref,
            created_at="2026-01-01T00:00:09Z",
        )
        assert new_recall.index == retry.index + 1
        assert new_recall.triggered_by == trigger.ref
        assert store.get_run_control(run_id=run.id, index=recall.index) is None
        assert retry.ref == ControlRef.for_run(run.id, retry.index)
    finally:
        store.close()
