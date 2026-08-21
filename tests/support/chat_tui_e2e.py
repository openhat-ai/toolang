"""Subprocess entry point for the terminal chat PTY system test."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from toolang.base.types.message import Message, TextDelta, TextPart
from toolang.base.types.run import (
    ModelCallResult,
    ModelPartDelta,
    ModelPartEnd,
    ModelPartStart,
)
from .chat_tui_runner import run_chat_tui
from .execution_harness import AsyncGate, ExecutionHarness, ScriptedModelTurn


class _DelayGate(AsyncGate):
    async def wait(self) -> None:
        await asyncio.sleep(0.5)


class _StatusDelayGate(AsyncGate):
    async def wait(self) -> None:
        await asyncio.sleep(1.5)


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
                    ModelPartStart(kind="text"),
                    *(
                        ModelPartDelta(delta=TextDelta(line))
                        for line in response.splitlines(keepends=True)
                    ),
                    ModelPartEnd(data=TextPart(response)),
                ),
                after_updates_gate=_DelayGate(),
            )
        ]
    elif mode == "status":
        responses = [
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant(response)),
                gate=_StatusDelayGate(),
            )
        ]
    else:
        responses = [ModelCallResult(message=Message.assistant(response))]
    harness = ExecutionHarness.create(
        root,
        source="""
agic chat(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}

flow relay(_: Part[]) -> Part[]:
  run chat
""",
        responses=responses,
        streaming=long_output,
    )
    harness.store.close()

    selects: dict[str, object] = {"flow": "relay"} if mode == "flow" else {}
    run_chat_tui(harness.setup, harness.state, selects=selects)


if __name__ == "__main__":
    main()
