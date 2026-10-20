"""Public runnable modules execute with their private Program context."""

import asyncio

import pytest

from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult, ToolCall
from toolang.common.layout import AgentLayout
from toolang.execution.types import ThreadKind
from toolang.state.prepare import prepare_agent_state


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize("named", [False, True])
def test_root_flow_calls_export_with_private_helper(tmp_path, kind, named):
    layout = AgentLayout.resident(tmp_path, "alice")
    (layout.home / "runnables").mkdir(parents=True)
    root_source = "flow parent(_: Text) -> Text:\n  run research\n"
    layout.program.write_text(root_source)
    module_source = (
        f"{kind}{' research' if named else ''}(_: Text, extra?: Text) -> Text:\n"
        + (
            "  hands = helper\n  Use the local helper. {{_}} {{#extra}}{{extra}}{{/extra}}\n"
            if kind == "agic"
            else "  run helper\n"
        )
        + "agic helper(_: Text) -> Text:\n  Private module guidance. {{_}}\n"
    )
    (layout.home / "runnables/research.too").write_text(module_source)
    state = prepare_agent_state(layout)
    answer = ModelCallResult(message=Message.assistant("done"))
    responses = (
        [
            ModelCallResult(
                tool_calls=(
                    ToolCall(
                        "helper",
                        "helper",
                        "runtime__run",
                        {"runnable": "helper", "input": {"_": "input"}},
                    ),
                )
            ),
            answer,
            answer,
        ]
        if kind == "agic"
        else [answer]
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source=root_source,
        state=state,
        program=state.modules["agent"],
        responses=responses,
    )

    async def scenario():
        async with harness:
            root = await harness.executor.run(
                harness.run_spec(
                    thread=harness.threads.create(kind=ThreadKind.CHAT),
                    runnable="parent",
                    primary=(TextPart("input"),),
                )
            )
            assert root.status == "succeeded", root.error
            assert harness.store.run_output_text(run_id=root.id) == "done"
            runs = harness.store.list_run_tree(root_run_id=root.id)
            assert len(runs) == 3
            assert all(run.status == "succeeded" for run in runs)
            helper_call = harness.adapter.invocations[1 if kind == "agic" else 0].call
            assert "Private module guidance." in str(helper_call.messages)
            assert harness.adapter.pending_responses == 0

    asyncio.run(scenario())
