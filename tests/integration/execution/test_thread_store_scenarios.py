"""Thread history and durable replay correctness scenarios."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3

import pytest

from tests.support.execution_fixtures import (
    project_run_end,
    project_run_start,
    project_step,
)
from tests.support.execution_harness import ExecutionHarness, RecordingTool
from toolang.base.types.message import (
    Message,
    ToolCallPart,
    ToolResultPart,
)
from toolang.base.types.run import ModelCall, ModelCallResult, ToolCall
from toolang.base.types.tool import ToolDefinition
from toolang.execution.history import RunHistory
from toolang.execution.records import StepRecord, model_call_to_data
from toolang.execution.store import RunStore
from toolang.execution.types import StepPath, ThreadPrefix
from toolang.lang.input import perceive_input


def _capture_replayable_model_step(store: RunStore) -> StepRecord:
    given = store.capture_model_call(
        target={
            "ref": "test/model",
            "provider": "test",
            "name": "model",
            "model": "model",
            "adapter": "test",
            "base_url": None,
            "scope": None,
            "tags": [],
            "options": {},
            "tools": True,
            "streaming": False,
        },
        call=ModelCall(
            instructions="stable instructions",
            messages=[Message.user("hello")],
            tools=(
                ToolDefinition(
                    name="test__lookup",
                    description="Look up one value.",
                    parameters={"type": "object"},
                ),
            ),
        ),
    )
    return store.begin_step(
        path=StepPath("run_replayable_model", (0,)),
        kind="model",
        input=(),
        given=given,
        started_at="2026-01-01T00:00:00Z",
    )


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
                    primary=perceive_input("question"),
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
        assert record is not None and record.status == "succeeded"
        steps = reopened.list_steps(run_id=run_id)
        assert len(steps) == 1
        assert reopened.rebuild_model_call(steps[0]) == expected_call
        assert reopened.recent_conversation_messages(thread_id=thread_id) == [
            Message.user("question"),
            Message.assistant("answer"),
        ]
        detail = RunHistory(reopened).get_run(run_id)
        assert detail is not None
        assert detail.steps[0].given["call"] == model_call_to_data(expected_call)
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
                    primary=perceive_input("first question"),
                )
            )
            second = await harness.executor.start(
                harness.run_spec(
                    thread=thread,
                    runnable="chat",
                    primary=perceive_input("second question"),
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
                    primary=perceive_input("branch question"),
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
            assert (
                harness.store.list_thread_history_chronological(thread_id=thread) == ()
            )
            assert second.status == "succeeded"
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
                    primary=perceive_input("double three"),
                )
            )

            assert record.status == "succeeded"
            assert harness.adapter.invocations[1].call.state == {"cursor": "turn-1"}
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
                tuple(invocation.call for invocation in harness.adapter.invocations),
            )

    run_id, expected_calls = asyncio.run(scenario())

    reopened = RunStore(harness.store.db_path)
    try:
        model_steps = [
            step for step in reopened.list_steps(run_id=run_id) if step.kind == "model"
        ]
        assert len(model_steps) == len(expected_calls) == 2
        assert (
            tuple(reopened.rebuild_model_call(step) for step in model_steps)
            == expected_calls
        )
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


@pytest.mark.parametrize(
    ("table", "column", "error"),
    [
        ("model_texts", "body", "model text is corrupted"),
        ("model_messages", "data", "model message is corrupted"),
        ("model_toolsets", "data", "model toolset is corrupted"),
    ],
)
def test_rebuild_model_call_rejects_corrupted_content_addressed_data(
    tmp_path: Path,
    table: str,
    column: str,
    error: str,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        step = _capture_replayable_model_step(store)
        connection = sqlite3.connect(store.db_path)
        try:
            connection.execute(
                f"UPDATE {table} SET {column} = ?",
                ("corrupted",),
            )
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(ValueError, match=error):
            store.rebuild_model_call(step)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("table", "error"),
    [
        ("model_texts", "model instructions are missing"),
        ("model_messages", "model message is missing"),
        ("model_toolsets", "model toolset is missing"),
    ],
)
def test_rebuild_model_call_rejects_missing_content_addressed_data(
    tmp_path: Path,
    table: str,
    error: str,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        step = _capture_replayable_model_step(store)
        connection = sqlite3.connect(store.db_path)
        try:
            connection.execute(f"DELETE FROM {table}")
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(ValueError, match=error):
            store.rebuild_model_call(step)
    finally:
        store.close()


def test_recent_history_never_splits_a_complete_tool_exchange(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_tool_history",
            thread_id="term_tool_history",
            origin="chat",
            input=Message.user("calculate"),
        )
        call_parts = (
            ToolCallPart(
                tool_call_id="tool-1",
                call_id="call-1",
                tool_name="test__first",
                tool_family="test__first",
                input={},
            ),
            ToolCallPart(
                tool_call_id="tool-2",
                call_id="call-2",
                tool_name="test__second",
                tool_family="test__second",
                input={},
            ),
        )
        result_messages = tuple(
            (
                Message(
                    role="tool",
                    parts=(
                        ToolResultPart(
                            tool_call_id=call.tool_call_id,
                            call_id=call.call_id,
                            tool_name=call.tool_name,
                            tool_family=call.tool_family,
                            output={"ok": True},
                        ),
                    ),
                )
            )
            for call in call_parts
        )
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="model",
            status="succeeded",
            input=(),
            output=call_parts,
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        for index, message in enumerate(result_messages, start=1):
            project_step(
                store,
                run_id=run.id,
                step_index=index,
                kind="tool",
                status="succeeded",
                input=(),
                output=message.parts,
                started_at=f"2026-01-01T00:00:0{index + 2}Z",
                finished_at=f"2026-01-01T00:00:0{index + 3}Z",
            )
        final = Message.assistant("done")
        project_step(
            store,
            run_id=run.id,
            step_index=3,
            kind="model",
            status="succeeded",
            input=(),
            output=final.parts,
            started_at="2026-01-01T00:00:05Z",
            finished_at="2026-01-01T00:00:06Z",
        )
        project_run_end(store, run_id=run.id)

        complete = store.recent_conversation_messages(
            thread_id=run.thread,
            limit=4,
        )
        assert complete == [
            Message(role="assistant", parts=call_parts),
            *result_messages,
            final,
        ]
        assert store.recent_conversation_messages(
            thread_id=run.thread,
            limit=3,
        ) == [final]
    finally:
        store.close()


def test_recent_history_skips_incomplete_and_orphan_tool_messages(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs.db")
    try:
        run = project_run_start(
            store,
            run_id="run_invalid_tool_history",
            thread_id="term_invalid_tool_history",
            origin="chat",
            input=Message.user("calculate"),
        )
        project_step(
            store,
            run_id=run.id,
            step_index=0,
            kind="model",
            status="succeeded",
            input=(),
            output=(
                ToolCallPart(
                    tool_call_id="missing-result",
                    tool_name="test__missing",
                    tool_family="test__missing",
                    input={},
                ),
            ),
            started_at="2026-01-01T00:00:01Z",
            finished_at="2026-01-01T00:00:02Z",
        )
        project_step(
            store,
            run_id=run.id,
            step_index=1,
            kind="tool",
            status="succeeded",
            input=(),
            output=(
                ToolResultPart(
                    tool_call_id="missing-result",
                    tool_name="test__missing",
                    tool_family="test__missing",
                    output={},
                ),
                ToolResultPart(
                    tool_call_id="orphan",
                    tool_name="test__orphan",
                    tool_family="test__orphan",
                    output={},
                ),
            ),
            started_at="2026-01-01T00:00:03Z",
            finished_at="2026-01-01T00:00:04Z",
        )
        final = Message.assistant("safe")
        project_step(
            store,
            run_id=run.id,
            step_index=2,
            kind="model",
            status="succeeded",
            input=(),
            output=final.parts,
            started_at="2026-01-01T00:00:05Z",
            finished_at="2026-01-01T00:00:06Z",
        )
        project_run_end(store, run_id=run.id)

        assert store.recent_conversation_messages(thread_id=run.thread) == [
            Message.user("calculate"),
            final,
        ]
    finally:
        store.close()
