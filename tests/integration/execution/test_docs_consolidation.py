"""Documentation discovery hands compact paths to per-file inspection."""

import asyncio
import json
from pathlib import Path

from tests import PROJECT_ROOT
from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import Message, message_text
from toolang.base.types.run import ModelCallResult
from toolang.execution.types import ThreadPrefix


def test_discovered_paths_are_valid_inspection_inputs(tmp_path: Path) -> None:
    source = (PROJECT_ROOT / "examples/docs_consolidation.too").read_text()
    paths = ["README.md", "docs/plans/unfinished.md"]
    reviews = [
        {
            "run_id": f"review-{index}",
            "path": path,
            "passed": False,
            "concepts": [],
            "evidence": [],
            "findings": [],
            "checks": ["Implementation still needs review"],
        }
        for index, path in enumerate(paths)
    ]
    harness = ExecutionHarness.create(
        tmp_path,
        source=source,
        responses=[
            ModelCallResult(message=Message.assistant(json.dumps(value)))
            for value in [paths, *reviews]
        ],
    )

    async def scenario() -> None:
        async with harness:
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            named = {"repo": "code", "out": "docs"}
            discovered = await harness.executor.run(
                harness.run_spec(thread=thread, runnable="discover", named=named)
            )
            assert discovered.status == "succeeded", discovered.error
            items = json.loads(harness.store.run_output_text(run_id=discovered.id))
            assert items == paths

            for path, expected in zip(items, reviews, strict=True):
                inspected = await harness.executor.run(
                    harness.run_spec(
                        thread=thread, runnable="inspect", primary=path, named=named
                    )
                )
                assert inspected.status == "succeeded", inspected.error
                assert (
                    json.loads(harness.store.run_output_text(run_id=inspected.id))
                    == expected
                )
                prompt = "\n".join(
                    message_text(message.parts)
                    for message in harness.adapter.invocations[-1].call.messages
                )
                assert f"document: {path}." in prompt
            assert harness.adapter.pending_responses == 0

    asyncio.run(scenario())
