"""Execution contracts for indentation ownership supplied by the grammar."""

import asyncio
from pathlib import Path

import pytest

from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import Message, message_text
from toolang.base.types.run import ModelCallResult
from toolang.execution.types import LoopStepNoted, ThreadPrefix
from toolang.lang.input import resolve_input_parts


@pytest.mark.parametrize("conditional", [False, True])
def test_block_ownership_preserves_iteration_counts_and_prompt_boundaries(
    tmp_path: Path, conditional: bool
) -> None:
    if conditional:
        body = """  repeat:
    repeat 2 times:
      run -> Text:
        Improve the evidence.
        sort is literal prompt content.
    run -> Text: Review the evidence.
    until: Return true when complete.
"""
        outputs = ["one", "two", "reviewed", "true", "published"]
        prompts = [
            "Improve the evidence.\nsort is literal prompt content.",
            "Improve the evidence.\nsort is literal prompt content.",
            "Review the evidence.",
            "Return true when complete.",
            "Publish the result.",
        ]
    else:
        body = """  repeat 2 times:
    run -> Text: Improve the evidence.
"""
        outputs = ["one", "two", "published"]
        prompts = [
            "Improve the evidence.",
            "Improve the evidence.",
            "Publish the result.",
        ]
    harness = ExecutionHarness.create(
        tmp_path,
        source="flow research(_: Text) -> Text:\n"
        + body
        + "  run -> Text: Publish the result.\n",
        responses=[
            ModelCallResult(message=Message.assistant(text)) for text in outputs
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="research",
                    primary=resolve_input_parts("question"),
                )
            )
            assert root.status == "succeeded", root.error
            assert harness.store.run_output_text(run_id=root.id) == "published"
            steps = harness.store.list_steps(run_id=root.id)
            assert [step.kind for step in steps if step.parent is None] == [
                "loop",
                "run",
            ]
            assert [
                message_text(invocation.call.messages[-1].parts).split(
                    "</toolang:context>\n\n", 1
                )[-1]
                for invocation in harness.adapter.invocations
            ] == prompts
            loops = [step.noted for step in steps if step.kind == "loop"]
            assert loops == (
                [
                    LoopStepNoted(iterations=1, termination="satisfied", total=None),
                    LoopStepNoted(iterations=2, termination="exhausted", total=2),
                ]
                if conditional
                else [LoopStepNoted(iterations=2, termination="exhausted", total=2)]
            )
            assert harness.adapter.pending_responses == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("nested", [False, True])
def test_until_receives_locals_defined_in_its_body(
    tmp_path: Path, nested: bool
) -> None:
    body = (
        "    repeat 1 time:\n      let evidence = Reviewed evidence.\n"
        if nested
        else "    let evidence = Reviewed evidence.\n"
    )
    harness = ExecutionHarness.create(
        tmp_path,
        source="flow research:\n  repeat 2 times:\n"
        + body
        + "    until: Is {{evidence}} sufficient?\n",
        responses=[ModelCallResult(message=Message.assistant("true"))],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="research",
                    primary=resolve_input_parts("question"),
                )
            )
            assert root.status == "succeeded", root.error
            assert len(harness.adapter.invocations) == 1
            prompt = message_text(
                harness.adapter.invocations[0].call.messages[-1].parts
            )
            assert "Reviewed evidence." in prompt
            loop = harness.store.list_steps(run_id=root.id)[0]
            assert loop.noted == LoopStepNoted(
                iterations=1, termination="satisfied", total=2
            )

    asyncio.run(scenario())
