"""Model name recovery is advice, never an implicit call or authority grant."""

import asyncio

import pytest

from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import Message, ToolResultPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.plugin.toolsets.loading import LoadedTool
from toolang.plugin.toolsets.registry import ToolRef
from toolang.execution.types import ThreadKind


@pytest.mark.parametrize(
    "name,suggestion",
    [
        ("issues.list", "issues__list"),
        ("issues/list", "issues__list"),
        ("issues.lsit", None),
        ("unavailable/list", None),
        ("runtime.honor", None),
        ("runtime.compact", None),
        ("me/list", None),
        ("msg/send", None),
        ("_toolang/run", None),
    ],
)
def test_unknown_name_keeps_original_exchange_and_does_not_execute(
    tmp_path, name, suggestion
):
    calls = []

    @tool(description="List issues")
    def list_issues(project: str):
        calls.append(project)
        return {"items": []}

    leaf = create_function_tool(list_issues)
    loaded = LoadedTool("vendor", "external", ToolRef("vendor", "issues", "list"), leaf)
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic task():\n  Check names.\n",
        tools={"issues__list": loaded},
        responses=[
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "call-original",
                        "provider-original",
                        name,
                        {"project": "toolang"},
                    ),
                )
            ),
            ModelCallResult(message=Message.assistant("Done")),
        ],
    )

    async def scenario():
        async with harness:
            record = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.CHAT),
                    runnable="task",
                )
            )
            assert record.status == "succeeded"
            steps = harness.store.list_steps(run_id=record.id)
            output = next(s.output for s in steps if s.kind == "tool")
            assert output is not None
            result = output.value
            assert isinstance(result, ToolResultPart)
            assert (
                result.tool_call_id == "call-original"
                and result.call_id == "provider-original"
            )
            assert result.tool_name == name
            assert result.error is not None
            assert (
                (f"call {suggestion!r} instead" in result.error)
                if suggestion
                else "instead" not in result.error
            )
            assert calls == []

    asyncio.run(scenario())
