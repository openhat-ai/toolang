"""Real compact lifecycle events render consistently in Chat and Script."""

import asyncio
from dataclasses import replace
from io import StringIO
from typing import Any, cast

import re
import pytest
from rich.console import Console

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    ScriptedModelTurn,
)
from tests.support.setup import replace_materialized_setup
from toolang.base.model_settings import parse_model_body
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.cli.common.script_progress import ScriptRunPresenter
from toolang.cli.toolang.commands.chat.presenter import ChatRunPresenter
from toolang.execution.events import RunTracer
from toolang.execution.types import ThreadPrefix


class ChatScreen:
    def __init__(self):
        self.active = None
        self.live = []
        self.finalized = []

    def get_active_run(self):
        return self.active

    def set_active_run(self, run_id):
        self.active = run_id

    def get_live_blocks(self):
        return self.live

    def finalize_block(self, block):
        self.live[:] = [item for item in self.live if item is not block]
        self.finalized.append(block)

    def finish_run(self):
        self.active = None

    def text(self, width):
        output = StringIO()
        console = Console(file=output, width=width, color_system=None)
        for block in [*self.finalized, *self.live]:
            console.print(block.render())
        return output.getvalue()


class Terminal(StringIO):
    def isatty(self):
        return True


def terminal_text(output, width):
    if not output.isatty():
        return output.getvalue()
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output.getvalue())


@pytest.mark.parametrize("status", ["succeeded", "failed", "canceled"])
@pytest.mark.parametrize("tty", [False, True])
@pytest.mark.parametrize("width", [40, 100])
def test_compaction_progress_in_chat_and_script(tmp_path, status, tty, width):
    def reply(text):
        return ModelCallResult(message=Message.assistant(text))

    h = ExecutionHarness.create(
        tmp_path,
        source="""agic chat(_: Part[]) -> Text:
  context = none
  instruct = none
  user: {{_}}
""",
        responses=[reply("old " * 18000), reply("recent")],
    )
    gate = AsyncGate()
    output = Terminal() if tty else StringIO()
    script = ScriptRunPresenter(run_id=None, stream=output, width=width)
    chat, app = ChatRunPresenter(max_width=width), ChatScreen()

    class Tracer(RunTracer):
        async def on_event(self, event):
            await script.on_event(event)
            chat.handle(event, cast(Any, app))

    async def scenario():
        async with h:
            thread = h.threads.create(prefix=ThreadPrefix.TERM)
            for text in ("old", "recent"):
                await h.executor.run(
                    h.run_spec(
                        thread=thread, runnable="chat", primary=(TextPart(text),)
                    )
                )
            normal = replace(
                h.setup.models_effective()[0], limit={"context": 14000, "output": 512}
            )
            reducer = replace(
                normal,
                id="reducer",
                name="reducer",
                limit={"context": 200000, "output": 8192},
            )
            h.setup = replace_materialized_setup(
                h.setup,
                models=(normal, reducer),
                compact_model=parse_model_body("test/reducer"),
            )
            h.adapter._responses.extend(
                [
                    ScriptedModelTurn(
                        reply("HIDDEN_COMPACT_SUMMARY"),
                        gate=gate,
                        error=RuntimeError("Provider unavailable")
                        if status == "failed"
                        else None,
                    ),
                    reply("VISIBLE_FINAL_ANSWER"),
                ]
            )
            handle = h.executor.run(
                h.run_spec(
                    thread=thread, runnable="chat", primary=(TextPart("continue"),)
                ),
                tracer=Tracer(),
            )
            try:
                await asyncio.wait_for(gate.wait_until_entered(), 3)
                for text in (terminal_text(output, width), app.text(width)):
                    assert "Compacting thread history" in text
                    assert "HIDDEN_COMPACT_SUMMARY" not in text
                if status == "canceled":
                    handle.cancel()
                else:
                    gate.release()
                result = await asyncio.wait_for(handle, 3)
                assert result.status == status
                assert not script._projector._broken and not chat._projector._broken
                assert script._refresh_task is None
                assert not app.live and app.active is None
                for surface, text in (
                    ("script", terminal_text(output, width)),
                    ("chat", app.text(width)),
                ):
                    assert "HIDDEN_COMPACT_SUMMARY" not in text
                    assert "compact_read" not in text and "_:compact" not in text
                    if status == "succeeded":
                        assert "VISIBLE_FINAL_ANSWER" in text
                        if not tty or surface == "chat":
                            assert text.count("VISIBLE_FINAL_ANSWER") == 1
                        assert "Compacted thread history" in text
                        if not tty or surface == "chat":
                            assert text.count("Compacted thread history") == 1
                    else:
                        assert "VISIBLE_FINAL_ANSWER" not in text
                        assert (
                            "Failed" in text
                            if status == "failed"
                            else "Canceled" in text
                        )
                        assert text.count("Provider unavailable") == int(
                            status == "failed"
                        )
                    (tmp_path / f"{surface}.txt").write_text(text)
            finally:
                script.close()

    asyncio.run(scenario())
