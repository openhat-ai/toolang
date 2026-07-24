"""Thread history and durable replay correctness scenarios."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3

from tests.support.execution_harness import ExecutionHarness, RecordingTool
from toolang.base.types.message import (
    Message,
    ToolCallPart,
    ToolResultPart,
)
from toolang.base.types.run import ModelCall, ModelCallResult, ToolCall
from toolang.execution.inspection import ExecutionInspection
from toolang.execution.store import RunStore
from toolang.execution.types import ThreadPrefix
from toolang.lang.input import perceive_input


def test_run_store_reopens_with_replayable_model_calls_and_history(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic chat(_: Part[]) -> Part[]:
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[ModelCallResult(message=Message.assistant("answer"))],
    )

    async def scenario() -> tuple[str, str, ModelCall]:
        async with harness:
            thread_id = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread_id,
                    runnable="chat",
                    input=perceive_input("question"),
                )
            )
            return (
                record.id,
                thread_id,
                harness.adapter.invocations[0].call,
            )

    run_id, thread_id, expected_call = asyncio.run(scenario())

    reopened = RunStore(harness.store.db_path)
    try:
        record = reopened.get_run(run_id=run_id)
        assert record is not None and record.status == "finished"
        steps = reopened.list_steps(run_id=run_id)
        assert len(steps) == 1
        assert reopened.rebuild_model_call(steps[0]) == expected_call
        assert reopened.recent_conversation_messages(
            thread_id=thread_id
        ) == [
            Message.user("question"),
            Message.assistant("answer"),
        ]
        detail = ExecutionInspection(reopened).run_detail(run_id)
        assert detail is not None
        assert detail.steps[0].given["call"] == expected_call.to_data()
    finally:
        reopened.close()


def test_thread_history_supports_followup_fork_and_rewind(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic chat(_: Part[]) -> Part[]:
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            ModelCallResult(message=Message.assistant("first answer")),
            ModelCallResult(message=Message.assistant("second answer")),
            ModelCallResult(message=Message.assistant("branch answer")),
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            first = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="chat",
                    input=perceive_input("first question"),
                )
            )
            second = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="chat",
                    input=perceive_input("second question"),
                )
            )
            assert harness.adapter.invocations[1].call.messages == [
                Message.user("first question"),
                Message.assistant("first answer"),
                Message.user("second question"),
            ]

            branch = harness.threads.fork(
                thread_id=thread,
                run_id=first.id,
            )
            branch_run = await harness.executor.start(
                harness.run_spec(
                    thread=branch,
                    runnable="chat",
                    input=perceive_input("branch question"),
                )
            )
            assert harness.adapter.invocations[2].call.messages == [
                Message.user("first question"),
                Message.assistant("first answer"),
                Message.user("branch question"),
            ]
            assert [
                run.id
                for run in harness.store.list_thread_history_chronological(
                    thread_id=branch
                )
                if run.parent is None
            ] == [first.id, branch_run.id]

            harness.threads.rewind(thread_id=thread, run_id=first.id)
            assert harness.store.list_thread_history_chronological(
                thread_id=thread
            ) == ()
            assert second.status == "finished"
            assert harness.adapter.pending_responses == 0

    asyncio.run(scenario())


def test_reopened_store_rebuilds_stateful_tool_loop_without_duplicate_blobs(
    tmp_path: Path,
) -> None:
    tool = RecordingTool("math__double", output={"value": 6})
    call = ToolCall(
        tool_call_id="tool-1",
        call_id="call-1",
        name=tool.name,
        input={"value": 3},
    )
    call_part = ToolCallPart(
        tool_call_id=call.tool_call_id,
        call_id=call.call_id,
        tool_name=call.name,
        tool_family=call.name,
        input=call.input,
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic calculate(_: Text) -> Text:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[
            ModelCallResult(
                message=Message(role="assistant", parts=(call_part,)),
                tool_calls=(call,),
                state={"cursor": "turn-1"},
            ),
            ModelCallResult(
                message=Message.assistant("six"),
                state={"cursor": "turn-2"},
            ),
        ],
        tools={tool.name: tool},
    )

    async def scenario() -> tuple[str, tuple[ModelCall, ...]]:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            record = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="calculate",
                    input=perceive_input("double three"),
                )
            )

            assert record.status == "finished"
            assert harness.adapter.invocations[1].call.state == {
                "cursor": "turn-1"
            }
            assert harness.adapter.invocations[1].call.messages[-1].parts == (
                ToolResultPart(
                    tool_call_id=call.tool_call_id,
                    call_id=call.call_id,
                    tool_name=call.name,
                    tool_family=call.name,
                    output={"value": 6},
                ),
            )
            return (
                record.id,
                tuple(
                    invocation.call
                    for invocation in harness.adapter.invocations
                ),
            )

    run_id, expected_calls = asyncio.run(scenario())

    reopened = RunStore(harness.store.db_path)
    try:
        model_steps = [
            step
            for step in reopened.list_steps(run_id=run_id)
            if step.kind == "model"
        ]
        assert len(model_steps) == len(expected_calls) == 2
        assert tuple(
            reopened.rebuild_model_call(step)
            for step in model_steps
        ) == expected_calls
        assert model_steps[0].noted["state"] == {"cursor": "turn-1"}
        assert model_steps[1].noted["state"] == {"cursor": "turn-2"}

        connection = sqlite3.connect(reopened.db_path)
        try:
            assert connection.execute(
                "SELECT COUNT(*) FROM model_texts"
            ).fetchone() == (1,)
            assert connection.execute(
                "SELECT COUNT(*) FROM model_messages"
            ).fetchone() == (3,)
            assert connection.execute(
                "SELECT COUNT(*) FROM model_toolsets"
            ).fetchone() == (1,)
        finally:
            connection.close()
    finally:
        reopened.close()
