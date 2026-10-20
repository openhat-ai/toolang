"""Resident snapshots reflect actual work without history or record reads."""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from dataclasses import FrozenInstanceError

import pytest

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingTool,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult, ModelUsage, ToolCall
from toolang.execution.client import LocalRunClient
from toolang.execution.events import RunEnd
from toolang.execution.schemas import RunNode, StepNode
from toolang.execution.types import (
    CallCounters,
    ModelStepReport,
    RunReport,
    ThreadKind,
    TokenUsage,
    ToolStepReport,
)


def test_model_is_live_before_result_and_snapshot_is_resident(tmp_path, monkeypatch):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic task(_: Text) -> Text:\n  user: {{_}}\n",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(
                    message=Message.assistant("done"),
                    usage=ModelUsage(
                        12,
                        5,
                        input_cache_read_tokens=3,
                        output_reasoning_tokens=2,
                        reported_cost=0.25,
                        reported_currency="USD",
                    ),
                ),
                gate=gate,
            )
        ],
    )

    async def scenario():
        async with harness:
            client = LocalRunClient(harness.executor)
            with pytest.raises(RuntimeError, match="disconnected"):
                await client.live_trees()
            await client.connect()
            thread = harness.threads.create(kind=ThreadKind.CHAT)
            handle = harness.executor.run(
                harness.run_spec(
                    thread=thread, runnable="task", primary=(TextPart("x" * 100_000),)
                )
            )
            pending = harness.executor.live_trees()
            assert len(pending.trees) == 1
            assert pending.trees[0].nodes[0].status == "pending"
            record = harness.store.get_run(run_id=handle.run_id)
            assert record is not None and record.report is None
            await asyncio.wait_for(gate.wait_until_entered(), 3)

            def forbidden(*args, **kwargs):
                raise AssertionError("live reads must not touch records/history/ring")

            with monkeypatch.context() as patch:
                for method in (
                    "get_run",
                    "get_step",
                    "list_steps",
                    "read_transaction",
                    "resolve_value",
                ):
                    patch.setattr(harness.store, method, forbidden)
                patch.setattr(client._history, "get_run", forbidden)
                patch.setattr(harness.executor.stream, "boundary", forbidden)
                snapshot = await client.live_trees(root_run_id=handle.run_id)
                assert snapshot.epoch == pending.epoch
                assert len(snapshot.trees) == 1
                root, step = snapshot.trees[0].nodes
                assert isinstance(root, RunNode) and isinstance(step, StepNode)
                assert root.calls == CallCounters(model_calls=1)
                assert root.usage is None and root.cost is None
                assert step.kind == "model" and step.invoked
                assert step.parent == root.id and root.parent is None
                assert (await client.live_trees(root_run_id="run_absent")).trees == ()
                with pytest.raises(ValueError):
                    await client.live_trees(root_run_id="bad")
                with pytest.raises(FrozenInstanceError):
                    setattr(root, "status", "failed")
            gate.release()
            result = await handle
            assert result.status == "succeeded"
            assert result.report is not None
            assert result.report == RunReport(
                CallCounters(1, 0), TokenUsage(12, 5, 3, 2), 0.25
            )
            model = harness.store.list_steps(run_id=result.id)[0]
            assert isinstance(model.report, ModelStepReport) and model.report.invoked
            assert model.report.usage == result.report.usage
            assert model.report.cost is not None and model.report.cost.amount == 0.25
            assert root.calls == CallCounters(1, 0) and root.status == "running"
            assert (await client.live_trees()).trees == ()
            await harness.executor.stop()
            with pytest.raises(RuntimeError, match="stopped"):
                await client.live_trees()

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_tool_entry_counts_once_including_failure_and_cancellation(tmp_path, outcome):
    gate = AsyncGate()
    tool = RecordingTool(
        "test__work",
        output={},
        gate=gate,
        error=RuntimeError("tool failed") if outcome == "failure" else None,
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic task() -> Text:\n  user: Work\n",
        tools={tool.name: tool},
        responses=[
            ModelCallResult(tool_calls=(ToolCall("t", "t", tool.name, {}),)),
            ModelCallResult(message=Message.assistant("done")),
        ],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.CHAT),
                    runnable="task",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 3)
            root, step = harness.executor.live_trees().trees[0].nodes
            assert isinstance(root, RunNode) and isinstance(step, StepNode)
            assert root.calls == CallCounters(1, 1)
            assert step.kind == "tool" and step.invoked
            if outcome == "cancel":
                harness.executor.cancel(run_id=handle.run_id)
            else:
                gate.release()
            result = await handle
            assert result.status == ("canceled" if outcome == "cancel" else "succeeded")
            assert result.report is not None
            assert result.report.calls == CallCounters(
                1 if outcome == "cancel" else 2, 1
            )
            saved = next(
                s
                for s in harness.store.list_steps(run_id=result.id)
                if s.kind == "tool"
            )
            assert isinstance(saved.report, ToolStepReport) and saved.report.invoked
            assert len(tool.calls) == 1
            assert not harness.executor.live_trees().trees

    asyncio.run(scenario())


@pytest.mark.parametrize("launch", ["run", "async run", "spawn"])
def test_descendant_totals_and_async_ancestors(tmp_path, launch):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source=f"""
flow parent(_: Text) -> Text:
  {"let job = " if launch != "run" else ""}{launch} child
  {"await job" if launch != "run" else ""}
agic child(_: Text) -> Text:
  user: {{_}}
""",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(
                    message=Message.assistant("done"),
                    usage=ModelUsage(8, 2, reported_cost=0, reported_currency="USD"),
                ),
                gate=gate,
            )
        ],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.CHAT),
                    runnable="parent",
                    primary=(TextPart("hello"),),
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 3)
            trees = harness.executor.live_trees().trees
            assert len(trees) == (2 if launch == "spawn" else 1)
            nodes = [node for tree in trees for node in tree.nodes]
            runs = [node for node in nodes if isinstance(node, RunNode)]
            child = next(node for node in runs if node.id != handle.run_id)
            parent = next(node for node in runs if node.id == handle.run_id)
            assert child.calls == CallCounters(1, 0)
            assert parent.calls == CallCounters(0 if launch == "spawn" else 1, 0)
            if launch != "spawn":
                source = next(node for node in nodes if node.id == child.parent)
                assert source.status == (
                    "succeeded" if launch == "async run" else "running"
                )
                assert list(nodes).index(source) < list(nodes).index(child)
            else:
                assert child.parent is None
            gate.release()
            result = await handle
            assert result.status == "succeeded"
            assert result.report is not None
            assert result.report.calls == parent.calls
            assert result.report.usage == (
                None if launch == "spawn" else TokenUsage(8, 2)
            )
            child_record = harness.store.get_run(run_id=child.id)
            assert child_record is not None
            assert child_record.report == RunReport(
                CallCounters(1, 0), TokenUsage(8, 2), 0
            )
            assert not harness.executor.live_trees().trees

    asyncio.run(scenario())


def test_pre_entry_cancellation_finishes_once_with_zero_work(tmp_path):
    harness = ExecutionHarness.create(
        tmp_path, source="flow task() -> Text:\n  pass\n", responses=[]
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.CHAT),
                    runnable="task",
                )
            )
            root = harness.executor._root_runs[handle.run_id]
            root.task.cancel()
            result = await handle
            assert result.status == "canceled"
            saved = harness.store.get_run(run_id=handle.run_id)
            assert saved is not None and saved.report == RunReport()
            assert not harness.executor.has_owned_work()
            assert not harness.executor.live_trees().trees
            with harness.executor.stream.boundary() as (_, _, frames):
                assert sum(isinstance(f.event, RunEnd) for f in frames) == 1

    asyncio.run(scenario())


def test_end_rollback_retains_live_facts_and_receipts_do_not_recount(
    tmp_path, monkeypatch
):
    from toolang.execution.executor.executor import _Execution

    harness = ExecutionHarness.create(
        tmp_path,
        source="agic task() -> Text:\n  user: Work\n",
        responses=[
            ModelCallResult(message=Message.assistant("done"), usage=ModelUsage(3, 2))
        ],
    )
    finish = _Execution.finish_step
    persist = harness.store.finish_step
    failed = False

    async def finish_with_rollback(execution, **facts: Any):
        nonlocal failed
        if facts["kind"] != "model":
            return await finish(execution, **facts)
        before = execution.executor.live_trees().trees[0]

        def fail_commit(**kwargs):
            persist(**kwargs)
            raise RuntimeError("injected terminal rollback")

        with monkeypatch.context() as patch:
            patch.setattr(harness.store, "finish_step", fail_commit)
            with pytest.raises(RuntimeError, match="terminal rollback"):
                await finish(execution, **facts)
        assert execution.executor.live_trees().trees[0] == before
        saved = harness.store.get_step(ref=facts["step"])
        assert saved is not None and saved.status == "running"
        assert before.nodes[0].calls == CallCounters(1, 0)
        assert before.nodes[0].usage == TokenUsage(3, 2)
        failed = True
        await finish(execution, **facts)
        # Repeat the same final persisted facts; no second contribution/publication.
        record = harness.store.get_step(ref=facts["step"])
        assert record is not None
        facts["report"] = record.report
        await finish(execution, **facts)

    monkeypatch.setattr(_Execution, "finish_step", finish_with_rollback)

    async def scenario():
        async with harness:
            result = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.CHAT),
                    runnable="task",
                )
            )
            assert result.status == "succeeded" and failed
            assert result.report is not None
            assert result.report.calls == CallCounters(1, 0)
            assert result.report.usage == TokenUsage(3, 2)
            with harness.executor.stream.boundary() as (_, _, frames):
                assert sum(f.event.type == "step_end" for f in frames) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [True, False])
def test_invoked_model_failure_or_cancel_keeps_one_call(tmp_path, failure):
    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic task() -> Text:\n  user: Work\n",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(),
                gate=gate,
                error=RuntimeError("provider failed") if failure else None,
            )
        ],
    )

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.CHAT),
                    runnable="task",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 3)
            if failure:
                gate.release()
            else:
                harness.executor.cancel(run_id=handle.run_id)
            result = await handle
            assert result.status == ("failed" if failure else "canceled")
            assert result.report == RunReport(CallCounters(1, 0))
            step = harness.store.list_steps(run_id=result.id)[0]
            assert isinstance(step.report, ModelStepReport) and step.report.invoked

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["part_begin", "part_end"])
def test_model_result_usage_survives_cancel_during_part_delivery(tmp_path, boundary):
    from tests.support.execution_harness import RecordingRunTracer

    harness = ExecutionHarness.create(
        tmp_path,
        source="agic task() -> Text:\n  user: Work\n",
        responses=[
            ModelCallResult(
                message=Message.assistant("done"),
                usage=ModelUsage(9, 4, reported_cost=0.125, reported_currency="USD"),
            )
        ],
    )
    samples = []

    class CancelAtPart(RecordingRunTracer):
        async def on_event(self, event):
            await super().on_event(event)
            if event.type == boundary:
                samples.append(harness.executor.live_trees().trees[0])
                harness.executor.cancel(run_id=samples[-1].root_run_id)
                await asyncio.Future()

    tracer = CancelAtPart()
    harness.intercept_events(tracer)

    async def scenario():
        async with harness:
            result = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.CHAT),
                    runnable="task",
                ),
                tracer=tracer,
            )
            assert result.status == "canceled"
            root = samples[0].nodes[0]
            assert isinstance(root, RunNode) and root.usage == TokenUsage(9, 4)
            assert result.report == RunReport(
                CallCounters(1, 0), TokenUsage(9, 4), 0.125
            )
            saved = harness.store.list_steps(run_id=result.id)[0]
            assert isinstance(saved.report, ModelStepReport)
            assert saved.report.invoked and saved.report.usage == TokenUsage(9, 4)
            assert saved.report.cost is not None and saved.report.cost.amount == 0.125

    asyncio.run(scenario())


def test_returned_usage_survives_final_stream_validation_failure(tmp_path):
    from toolang.base.types.run import ModelPartStart, ModelPartEnd

    harness = ExecutionHarness.create(
        tmp_path,
        source="agic task() -> Text:\n  user: Work\n",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(
                    message=Message.assistant("different final text"),
                    usage=ModelUsage(
                        9, 4, reported_cost=0.125, reported_currency="USD"
                    ),
                ),
                updates=(
                    ModelPartStart(0, "text"),
                    ModelPartEnd(0, TextPart("streamed text")),
                ),
            )
        ],
        streaming=True,
    )

    async def scenario():
        async with harness:
            result = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.CHAT),
                    runnable="task",
                )
            )
            assert result.status == "failed"
            assert result.error is not None
            assert "does not match authoritative" in harness.store.resolve_error(
                result.error
            )
            assert result.report == RunReport(
                CallCounters(1, 0), TokenUsage(9, 4), 0.125
            )
            saved = harness.store.list_steps(run_id=result.id)[0]
            assert isinstance(saved.report, ModelStepReport)
            assert saved.report.usage == TokenUsage(9, 4) and saved.report.invoked

    asyncio.run(scenario())


def test_terminal_statistics_do_not_deadlock_with_cross_thread_control(
    tmp_path, monkeypatch
):
    from toolang.execution.executor.executor import _Execution
    from toolang.common.time import utc_now
    from toolang.lang.input import CallInput
    from toolang.lang.types import Array

    gate = AsyncGate()
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic task() -> Text:\n  user: Work\n",
        responses=[
            ScriptedModelTurn(
                ModelCallResult(message=Message.assistant("done")), gate=gate
            )
        ],
    )
    harness.executor._control_poll_interval = 3600
    finish = harness.executor._finish_run
    finish_run = _Execution.finish_run
    observed = threading.Event()
    errors = []
    workers = []
    control = None

    def observe():
        try:
            assert control is not None
            harness.executor._observe_control(control)
        except BaseException as exc:
            errors.append(exc)
        finally:
            observed.set()

    def finish_during_control(publication, **facts):
        if not workers:
            # A control thread must enqueue delivery without reading ancestry
            # or holding the registry while completion owns the Store.
            assert harness.store._conn.in_transaction
            worker = threading.Thread(target=observe, daemon=True)
            workers.append(worker)
            worker.start()
            assert observed.wait(5), "control observation blocked on the Store"
        return finish(publication, **facts)

    async def accept_control_before_terminal(execution, **facts):
        nonlocal control
        if control is None:
            control = harness.store.accept_run_control(
                run_id=facts["run"],
                kind="steer",
                timing="immediate",
                input=CallInput({"_": Array("Part[]", (TextPart("late input"),))}),
                request_id=None,
                created_at=utc_now(),
            )
        return await finish_run(execution, **facts)

    async def scenario():
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.CHAT),
                    runnable="task",
                )
            )
            await asyncio.wait_for(gate.wait_until_entered(), 3)
            with monkeypatch.context() as patch:
                patch.setattr(_Execution, "finish_run", accept_control_before_terminal)
                patch.setattr(harness.executor, "_finish_run", finish_during_control)
                gate.release()
                result = await handle
                for worker in workers:
                    worker.join(5)
                    assert not worker.is_alive()
            assert not errors
            assert result.status == "succeeded", result.error
            assert result.report == RunReport(CallCounters(1, 0))
            assert control is not None
            saved = harness.store.get_run_control(run_id=result.id, index=control.index)
            assert saved is not None and saved.status == "wontapply"
            assert not harness.executor.live_trees().trees

    asyncio.run(scenario())
