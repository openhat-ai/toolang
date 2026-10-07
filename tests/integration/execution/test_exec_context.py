"""A transferred agic sees its committed identity despite prior chat requests."""

import asyncio

import pytest

from tests.support.execution_assertions import (
    assert_replayed,
    execution_snapshot,
    route_snapshots,
)
from tests.support.execution_harness import ExecutionHarness, RecordingRunTracer
from toolang.base.types.message import Message, TextPart, message_text
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.types import ThreadPrefix


@pytest.mark.parametrize("unnamed", [False, True])
@pytest.mark.parametrize("context", ["", "  context = none\n"])
def test_exec_successor_declares_current_identity_with_chat_history(
    tmp_path, unnamed, context
):
    caller = "<entry:1>" if unnamed else "chat"
    harness = ExecutionHarness.create(
        tmp_path,
        source=(
            f"agic{'' if unnamed else ' chat'}:\n  {{{{_}}}}\n"
            "agic test(_):\n"
            f"{context}  Confirm the agent is working and echo {{{{_}}}}.\n"
        ),
        prepare_state=True,
        responses=[
            ModelCallResult(message=Message.assistant("What text should I echo?")),
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "exec",
                        "exec",
                        "_toolang__exec",
                        {"runnable": "agic:test", "input": {"_": "SMOKE_MARKER"}},
                    ),
                ),
            ),
            ModelCallResult(
                tool_calls=(
                    ToolCall("cwd", "cwd", "_toolang__chdir", {"path": "lab://"}),
                ),
            ),
            ModelCallResult(message=Message.assistant("Confirmed: SMOKE_MARKER")),
        ],
    )
    tracer = RecordingRunTracer()

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            previous = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable=caller,
                    primary=(TextPart("run agic:test"),),
                ),
                tracer=tracer,
            )
            assert previous.status == "succeeded", previous.error
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable=caller,
                    primary=(TextPart("Run agic:test; choose the smoke test input."),),
                ),
                tracer=tracer,
            )
            assert root.status == "succeeded", root.error
            assert (
                harness.store.run_output_text(run_id=root.id)
                == "Confirmed: SMOKE_MARKER"
            )
            assert (
                len(harness.store.list_run_controls(run_id=root.id, kind="exec")) == 1
            )
            assert harness.store.list_run_tree(root_run_id=root.id) == [root]
            calls = [invocation.call for invocation in harness.adapter.invocations]
            for call in calls[:2]:
                assert execution_snapshot(call) == {
                    "runnable": f"agent::agic:{caller}",
                    "entered_by": "run",
                }
            for call in calls[2:]:
                assert execution_snapshot(call) == {
                    "runnable": "agent::agic:test",
                    "entered_by": "exec",
                }
                text = "\n".join(message_text(m.parts) for m in call.messages)
                assert "What text should I echo?" in text
                assert "Confirm the agent is working and echo SMOKE_MARKER." in text
                routes = route_snapshots(call)
                assert not any(item["ref"] == "agic:test" for item in routes["hands"])
                assert any(item["ref"] == "agic:test" for item in routes["handoffs"])

    asyncio.run(scenario())
    assert_replayed(harness.store.db_path, tracer.events)
