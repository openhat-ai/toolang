"""Complete flow pipelines use test-owned sources and deterministic model turns."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests import FIXTURES_ROOT
from tests.support.execution_assertions import without_route_snapshots
from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import Message, TextPart, message_text
from toolang.base.types.run import ModelCallResult
from toolang.execution.types import ThreadPrefix


@pytest.mark.parametrize("constraints", [None, "Two engineers, four weeks"])
def test_delivery_pipeline_preserves_inputs_and_accumulates_reviews(
    tmp_path: Path, constraints: str | None
) -> None:
    source = (FIXTURES_ROOT / "flows" / "delivery_pipeline.too").read_text(
        encoding="utf-8"
    )
    project = "Build a small issue tracker"
    workstreams = ["discovery", "design", "build", "test", "release"]
    plans = [f"plan-{name}" for name in workstreams]
    lenses = ["delivery", "security", "adoption"]
    reviews = [f"review-{name}" for name in lenses]
    revisions = ["revision-1", "revision-2", "revision-3"]
    responses = [
        json.dumps(workstreams),
        *plans,
        "draft",
        json.dumps(lenses),
        *reviews,
        *revisions,
        "improved",
        "final",
    ]
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[
            ModelCallResult(message=Message.assistant(text)) for text in responses
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            root = await harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="flow:delivery",
                    primary=(TextPart(project),),
                    named={"constraints": constraints} if constraints else None,
                )
            )
            assert root.status == "succeeded", root.error
            assert harness.store.run_output_text(run_id=root.id) == "final"
            assert len(harness.adapter.invocations) == 16
            assert harness.adapter.pending_responses == 0
            prompts = [
                message_text(
                    without_route_snapshots(invocation.call.messages)[-1].parts
                )
                for invocation in harness.adapter.invocations
            ]
            for prompt in prompts:
                assert f"project={project}" in prompt.splitlines()
                assert f"constraints={constraints or ''}" in prompt.splitlines()
            for prompt, workstream in zip(prompts[1:6], workstreams, strict=True):
                assert f"workstream={workstream}" in prompt.splitlines()
            # Gather receives every mapped plan in source order.
            assert json.loads(prompts[6].split("plans=", 1)[1]) == plans
            assert "draft=draft" in prompts[7].splitlines()
            for prompt, lens in zip(prompts[8:11], lenses, strict=True):
                assert "draft=draft" in prompt.splitlines()
                assert f"lens={lens}" in prompt.splitlines()
            # The seed is the saved draft; later reducer calls receive the prior
            # revision while the named draft remains unchanged.
            for prompt, review, previous in zip(
                prompts[11:14], reviews, ["draft", *revisions[:-1]], strict=True
            ):
                assert "draft=draft" in prompt.splitlines()
                assert f"current={review}" in prompt.splitlines()
                assert f"previous={previous}" in prompt.splitlines()
            assert "plan=revision-3" in prompts[14].splitlines()
            assert "plan=improved" in prompts[15].splitlines()

    asyncio.run(scenario())
