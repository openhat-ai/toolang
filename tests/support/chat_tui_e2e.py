"""Subprocess entry point for the terminal chat PTY system test."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import sys
from unittest.mock import patch

from toolang.base.types.message import Message, TextDelta, TextPart
from toolang.base.types.run import (
    ModelCallResult,
    ModelPartDelta,
    ModelPartEnd,
    ModelPartStart,
)
from .chat_tui_runner import run_chat_tui
from .setup import replace_materialized_setup
from toolang.base.model_settings import parse_model_body
from toolang.execution.types import ThreadPrefix
from .execution_harness import AsyncGate, ExecutionHarness, ScriptedModelTurn


class _DelayGate(AsyncGate):
    async def wait(self) -> None:
        await asyncio.sleep(0.5)


class _StatusGate(AsyncGate):
    def __init__(self, release_path: Path) -> None:
        super().__init__()
        self.release_path = release_path

    async def wait(self) -> None:
        while not self.release_path.exists():
            await asyncio.sleep(0.01)


def main() -> None:
    root = Path(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "agic"
    long_output = mode == "long-output"
    response = (
        "\n".join(f"terminal e2e line {index:03}" for index in range(100))
        if long_output
        else "hello from terminal e2e"
    )
    if long_output:
        responses = [
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant(response)),
                updates=(
                    ModelPartStart(part=0, kind="text"),
                    *(
                        ModelPartDelta(part=0, delta=TextDelta(line))
                        for line in response.splitlines(keepends=True)
                    ),
                    ModelPartEnd(part=0, data=TextPart(response)),
                ),
                after_updates_gate=_DelayGate(),
            )
        ]
    elif mode == "status":
        responses = [
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant(response)),
                gate=_StatusGate(root / "release-model"),
            )
        ]
    else:
        responses = [ModelCallResult(message=Message.assistant(response))]
    harness = ExecutionHarness.create(
        root,
        source="""
agic chat(_: Part[]) -> Part[]:
  recall = none
  context = none
  instruct = none
  user: {{_}}

flow relay(_: Part[]) -> Part[]:
  run chat
""".replace(
            "  recall = none\n",
            "" if mode.startswith("compact") else "  recall = none\n",
        ),
        responses=responses,
        streaming=long_output,
    )
    thread_id = None
    if mode.startswith("compact"):
        from toolang.execution import tokens

        patch.object(
            tokens, "text_tokens", lambda text: (len(text.encode("utf-8")) + 2) // 3
        ).start()

        async def seed_compact():
            thread = harness.threads.create(prefix=ThreadPrefix.TERM)
            harness.adapter._responses.clear()
            harness.adapter._responses.extend(
                [
                    ModelCallResult(message=Message.assistant("old " * 18000)),
                    ModelCallResult(message=Message.assistant("recent")),
                ]
            )
            for text in ("old", "recent"):
                result = await harness.executor.run(
                    harness.run_spec(
                        thread=thread, runnable="chat", primary=(TextPart(text),)
                    )
                )
                assert result.status == "succeeded"
            await harness.executor.stop()
            return thread

        thread_id = asyncio.run(seed_compact())
        normal = replace(
            harness.setup.models_effective()[0], limit={"context": 14000, "output": 512}
        )
        reducer = replace(
            normal,
            id="reducer",
            name="reducer",
            limit={"context": 200000, "output": 8192},
        )
        harness.setup = replace_materialized_setup(
            harness.setup,
            models=(normal, reducer),
            compact_model=parse_model_body("test/reducer"),
        )
        harness.adapter._responses.extend(
            [
                ScriptedModelTurn(
                    result=ModelCallResult(
                        message=Message.assistant("HIDDEN_COMPACT_SUMMARY")
                    ),
                    gate=_StatusGate(root / "release-compact"),
                    error=RuntimeError("Provider unavailable")
                    if mode == "compact-failure"
                    else None,
                ),
                ModelCallResult(message=Message.assistant("VISIBLE_FINAL_ANSWER")),
            ]
        )
    harness.store.close()

    selects: dict[str, object] = {"flow": "relay"} if mode == "flow" else {}
    run_chat_tui(harness.setup, harness.state, selects=selects, thread_id=thread_id)


if __name__ == "__main__":
    main()
