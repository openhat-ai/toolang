"""Subprocess entry point for resident-remote terminal Chat PTY coverage."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from typing import Any

import httpx

from toolang.api.app import create_app
from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult
from toolang.catalog import CapsManager, JobsManager
from toolang.cli.toolang.commands.chat.remote import RemoteChatSession
from toolang.cli.toolang.commands.chat.tui import ChatTuiApp
from toolang.up import AgentCore
from .execution_harness import ExecutionHarness


class _Snapshot:
    def __init__(self, value: object) -> None:
        self.value = value

    def current(self) -> Any:
        return self.value


def main() -> None:
    root = Path(sys.argv[1])
    harness = ExecutionHarness.create(
        root,
        source="""
agic chat(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[ModelCallResult(message=Message.assistant("hello from remote e2e"))],
    )
    harness.store.close()
    core = AgentCore(harness.setup.layout)
    core.setup = _Snapshot(harness.setup)
    core.state = _Snapshot(harness.state)
    app = create_app(
        core,
        CapsManager(core.layout),
        JobsManager(core.layout),
        cors_allowed_origins=(),
    )
    session = RemoteChatSession(
        "http://runtime.test:7001",
        expected_sandbox="host",
        transport=httpx.ASGITransport(app=app),
    )
    try:
        ChatTuiApp.run(
            thread_id=None,
            selects={},
            home=str(harness.setup.layout.home),
            input_history=None,
            client=session,
        )
    finally:
        session.close()
        asyncio.run(core.close())


if __name__ == "__main__":
    main()
