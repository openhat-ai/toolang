"""Real agent activity HTTP/SSE with deterministic model and tool execution."""

import asyncio
import json
import os
from pathlib import Path
import socket
import sys

from fastapi import FastAPI
from rich.console import Console
import uvicorn

from tests.support.execution_harness import (
    AsyncGate,
    ExecutionHarness,
    RecordingTool,
    ScriptedModelTurn,
)
from toolang.api.routers.activity import router
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult, ModelUsage, ToolCall
from toolang.cli.common.activity import watch
from toolang.execution.activity import ActivityReader
from toolang.execution.types import ThreadPrefix


class FileGate(AsyncGate):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    async def wait(self) -> None:
        while not self.path.exists():
            await asyncio.sleep(0.02)


async def main() -> None:
    root = Path(sys.argv[1])
    tool = RecordingTool(
        "math__double", output={"value": 6}, gate=FileGate(root / "release-tool")
    )
    harness = ExecutionHarness.create(
        root,
        source="""
agic worker(_: Text) -> Text:
  recall = none
  context = none
  instruct = none
  user: {{_}}

flow review(_: Text) -> Text:
  run worker
""",
        responses=[
            ModelCallResult(message=Message.assistant("Already complete")),
            ModelCallResult(
                tool_calls=(ToolCall("tool-1", "call-1", tool.name, {"value": 3}),),
                usage=ModelUsage(10, 5, reported_cost=0.25, reported_currency="USD"),
            ),
            ScriptedModelTurn(
                result=ModelCallResult(message=Message.assistant("Review complete")),
                gate=FileGate(root / "release-model"),
            ),
        ],
        tools={tool.name: tool},
    )
    async with harness:
        thread = harness.threads.create(prefix=ThreadPrefix.TERM)
        await harness.executor.run(
            harness.run_spec(
                thread=thread, runnable="worker", primary=(TextPart("Earlier work"),)
            )
        )
        execution = asyncio.ensure_future(
            harness.executor.run(
                harness.run_spec(
                    thread=thread,
                    runnable="review",
                    primary=(TextPart("Analyze configuration"),),
                )
            )
        )
        app = FastAPI()
        app.state.activity = ActivityReader(harness.store.db_path, "agent:alice")
        app.include_router(router, prefix="/api/v1")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            serving = asyncio.create_task(
                asyncio.to_thread(server.run, sockets=[listener])
            )
            try:
                while not server.started:
                    if serving.done():
                        await serving
                    await asyncio.sleep(0.01)
                (root / "activity-endpoint.json").write_text(
                    json.dumps({"endpoint": endpoint, "thread": thread})
                )
                hub = sys.argv[2:]
                await watch(
                    hub[0] if hub else endpoint,
                    agent=None if hub else "agent:alice",
                    backend=hub[1] if hub else None,
                    once=False,
                    console=Console(),
                    refresh=float(os.environ.get("TOOLANG_TEST_REFRESH", "0.1")),
                )
            finally:
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                server.should_exit = True
                await serving


if __name__ == "__main__":
    asyncio.run(main())
