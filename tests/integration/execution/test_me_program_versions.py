"""Self-authoring reports bound code separately from fresh model resources."""

from __future__ import annotations

import asyncio
from hashlib import sha256

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
)
from toolang.base.types.message import Message, ToolResultPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.execution.types import ThreadPrefix
from toolang.plugin.toolsets.loading import load_tools
from toolang.state.prepare import prepare_agent_state


def test_me_keeps_bound_digest_after_publication_and_model_refresh(tmp_path) -> None:
    source = "agic inspect():\n  tools = me/*\n  Inspect old source.\n"
    gate = AsyncGate()

    def read_call(index):
        return ModelCallResult(
            tool_calls=(
                ToolCall(
                    tool_call_id=f"read-{index}",
                    call_id=f"provider-{index}",
                    name="me__get",
                    input={"kind": "program"},
                ),
            )
        )

    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        prepare_state=True,
        tools=load_tools(queries=("me/*",)),
        responses=(
            ScriptedModelTurn(result=read_call(1), gate=gate),
            read_call(2),
            ModelCallResult(message=Message.assistant("done")),
        ),
    )
    latest = source.replace("old source", "new source")

    async def scenario():
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            handle = harness.executor.run(
                harness.run_spec(thread=thread, runnable="agic:inspect")
            )
            await gate.wait_until_entered()
            harness.setup.layout.program.write_text(latest)
            harness.published = prepare_agent_state(harness.setup.layout)
            gate.release()
            root = await handle
            assert root.status == "succeeded", root.error
            replies = [
                part
                for message in harness.adapter.invocations[-1].call.messages
                for part in message.parts
                if isinstance(part, ToolResultPart) and part.tool_name == "me__get"
            ]
            assert len(replies) == 2
            for reply in replies:
                assert reply.error is None
                assert reply.output["item"]["content"]["source"] == latest
                assert reply.output["version"] == {
                    "run_digest": sha256(source.encode()).hexdigest(),
                    "authored_digest": sha256(latest.encode()).hexdigest(),
                    "matches_run": False,
                }

    asyncio.run(scenario())
