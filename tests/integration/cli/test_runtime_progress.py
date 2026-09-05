"""Progress projection from actual runtime control and Tool Step events."""

import asyncio
from pathlib import Path
import sqlite3

import pytest

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingRunTracer,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, ToolResultPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.cli.common.execution_progress import ProgressProjector
from toolang.execution.events import StepBegin, StepEnd
from toolang.execution.types import ThreadPrefix, ToolStepGiven
from toolang.execution.values import parts_from_local
from tests.support.execution_assertions import assert_run_event_integrity


def test_execute_commit_failure_finishes_its_tool_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic caller() -> Text:
  handoffs = agic:target
  user: Caller.

agic target() -> Text:
  user: Target.
""",
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "execute",
                        "execute",
                        "_too__execute",
                        {"runnable": "agic:target"},
                    ),
                )
            )
        ],
    )
    tracer = RecordingRunTracer()

    def fail_commit(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    async def scenario() -> None:
        async with harness:
            monkeypatch.setattr(harness.store, "accept_execute_control", fail_commit)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="agic:caller",
                ),
                tracer=tracer,
            )
            assert root.status == "failed"
            steps = harness.store.list_steps(run_id=root.id)
            assert [(step.kind, step.status) for step in steps] == [
                ("model", "succeeded"),
                ("tool", "failed"),
            ]
            output = steps[1].output
            assert output is not None
            result = parts_from_local(output)[0]
            assert isinstance(result, ToolResultPart)
            assert result.error == "database is locked"
            assert result.tool_call_id == "execute"
            assert not harness.store.list_run_controls(run_id=root.id, kind="execute")
            assert len(harness.adapter.invocations) == 1
            assert_run_event_integrity(tracer.events)
            projector = ProgressProjector()
            for event in tracer.events:
                projector.handle(event)
            assert not projector._broken
            assert projector._root_ended

    asyncio.run(scenario())


@pytest.mark.parametrize("target_kind", ["agic", "flow"])
@pytest.mark.parametrize("outcome", ["succeeded", "failed", "canceled", "steered"])
def test_execute_progress_follows_runtime_events(
    tmp_path: Path, target_kind: str, outcome: str
) -> None:
    target = f"{target_kind}:target" if outcome != "failed" else "agic:missing"
    source = """
agic caller() -> Text:
  recall = none
  handoffs = target
  user: Caller.

agic child() -> Text:
  recall = none
  user: Child.
"""
    source += (
        "\nagic target() -> Text:\n  recall = none\n  hands = agic:child\n  user: Target.\n"
        if target_kind == "agic"
        else "\nflow target() -> Text:\n  run child\n"
    )
    gate = AsyncGate()
    responses = [
        ScriptedModelTurn(
            result=ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "execute", "execute", "_too__execute", {"runnable": target}
                    ),
                )
            ),
            gate=gate,
        ),
    ]
    if target_kind == "agic" and outcome == "succeeded":
        responses.append(
            ScriptedModelTurn(
                result=ModelCallResult(
                    tool_calls=(
                        ToolCall(
                            "child", "child", "_too__run", {"runnable": "agic:child"}
                        ),
                    )
                )
            )
        )
    responses.extend(
        [
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("done"))
            ),
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("finished"))
            ),
        ]
    )
    harness = ExecutionHarness.create(tmp_path, source=source, responses=responses)
    tracer = RecordingRunTracer()

    async def scenario() -> None:
        async with harness:
            handle = harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(prefix=ThreadPrefix.TERM),
                    runnable="agic:caller",
                ),
                tracer=tracer,
            )
            await asyncio.wait_for(gate.wait_until_entered(), timeout=1)
            if outcome == "canceled":
                handle.cancel(timing="next_step")
            elif outcome == "steered":
                handle.steer(Message.user("skip execute"), timing="next_step")
            gate.release()
            root = await asyncio.wait_for(handle, timeout=2)
            assert root.status == ("canceled" if outcome == "canceled" else "succeeded")

            projector = ProgressProjector()
            handoffs = []
            rows = []
            execute_step = None
            confirmed = False
            for event in tracer.events:
                if (
                    isinstance(event, StepBegin)
                    and isinstance(event.given, ToolStepGiven)
                    and event.given.call.name == "_too__execute"
                ):
                    execute_step = event.step
                if isinstance(event, StepEnd) and event.step == execute_step:
                    confirmed = event.status == "succeeded"
                update = projector.handle(event)
                for block in update.committed:
                    for row in block.rows:
                        rows.append(row)
                        if row.leader == "handoff":
                            assert confirmed
                            assert isinstance(event, StepBegin)
                            assert event.step != execute_step
                            handoffs.append(row)
            assert not projector._broken
            assert projector._root_ended
            if outcome == "succeeded":
                assert len(handoffs) == 1
                assert handoffs[0].text == f"---  handoff to {target}"
                if target_kind == "agic":
                    assert any(row.text == "---  run agic:child" for row in rows)
                    child = harness.store.list_run_tree(root_run_id=root.id)[1]
                    assert any(row.right_identity == child.id for row in rows)
                else:
                    assert any("Run child" in row.text for row in rows)
            else:
                assert handoffs == []
                if outcome in {"failed", "canceled"}:
                    assert any("Failed to execute" in row.text for row in rows)
                else:
                    assert execute_step is None

    asyncio.run(scenario())
