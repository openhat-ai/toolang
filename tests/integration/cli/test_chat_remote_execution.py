"""Terminal Chat execution through a resident AgentServer boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from toolang.api.app import create_app
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.catalog import CapsManager, JobsManager
from toolang.cli.toolang.commands.chat.base import RunAccepted
from toolang.cli.toolang.commands.chat.remote import RemoteChatSession
from toolang.execution.events import RunBegin, RunEnd, RunEvent
from toolang.up import AgentCore, process as agents
from tests.support.execution_harness import ExecutionHarness


_HOST_DESCRIPTION = "Test OS 1.0 arm64"


class _Snapshot:
    def __init__(self, value: object) -> None:
        self.value = value

    def current(self) -> Any:
        return self.value


def test_remote_chat_session_executes_against_the_agent_api(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic chat(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[ModelCallResult(message=Message.assistant("remote response"))],
    )
    harness.store.close()
    core = AgentCore(harness.setup.layout)
    core.setup = _Snapshot(harness.setup)
    core.state = _Snapshot(harness.state)
    agents.write_runtime_state(
        core.layout,
        endpoint="http://runtime.test:7001",
        started_at="2026-08-26T00:00:00Z",
        pid=123,
        sandbox_description=_HOST_DESCRIPTION,
    )
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
    events: list[RunEvent] = []
    states: list[object] = []
    errors: list[str] = []

    try:
        assert session.list_models()["default"] == "test/scripted[test]"
        assert session.list_executables("runnable")["default"] == "agic:chat"
        thread_id = session.create_thread()
        session.start_run(
            thread_id,
            "hello",
            {},
            events.append,
            errors.append,
            states.append,
        )
        result = session.get_result(None, thread_id=thread_id)

        assert isinstance(events[0], RunBegin)
        assert isinstance(events[-1], RunEnd)
        root_id = events[0].run
        assert states == [RunAccepted(root_id)]
        assert result.run_id == root_id
        assert result.output == (TextPart("remote response"),)
        assert errors == []
    finally:
        session.close()
        asyncio.run(core.close())
