"""Terminal Chat execution through a resident AgentServer boundary."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from toolang.api.app import create_app
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.catalog import CapsManager, JobsManager
from toolang.cli.toolang.commands.chat.base import RunAccepted, RunWorkdirUpdated
from toolang.cli.toolang.commands.chat.remote import RemoteChatSession
from toolang.execution.events import RunBegin, RunEnd, RunEvent
from toolang.execution.types import RunOverride
from toolang.lang.input import CallInput
from toolang.state.prepare import prepare_agent_state
from toolang.up import AgentCore, process as agents
from tests.support.execution_harness import ExecutionHarness


_HOST_DESCRIPTION = "Test OS 1.0 arm64"


class _Snapshot:
    def __init__(self, value: object) -> None:
        self.value = value

    def current(self) -> Any:
        return self.value

    async def refresh(self) -> object:
        raise AssertionError("remote run acceptance must not refresh publications")


@pytest.mark.parametrize("entry", ["agic:chat", "agic:_", "flow:_", "flow:chat"])
def test_remote_chat_session_executes_against_the_agent_api(
    tmp_path: Path, entry: str
) -> None:
    source = """
agic chat(_: Part[]) -> Part[]:
  recall = none
  context = none
  instruct = none
  user: {{_}}
"""
    if entry == "agic:_":
        source = source.replace("agic chat", "agic")
    elif entry in {"flow:_", "flow:chat"}:
        source = source.replace("agic chat", "agic helper")
        source += "\nflow(_: Part[]) -> Part[]:\n  run helper\n"
    harness = ExecutionHarness.create(
        tmp_path,
        source="" if entry == "flow:chat" else source,
        responses=[ModelCallResult(message=Message.assistant("remote response"))],
    )
    harness.store.close()
    core = AgentCore(harness.setup.layout)
    core.setup = _Snapshot(harness.setup)
    state = harness.state
    if entry == "flow:chat":
        flows = harness.setup.layout.home / "flows"
        flows.mkdir()
        (flows / "chat.too").write_text(source)
        state = prepare_agent_state(harness.setup.layout)
    core.state = _Snapshot(state)
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
        assert session.list_models()["default"] == "test/scripted"
        setting = session.initial_setting().runnable or ""
        if entry.endswith(":_"):
            assert setting.startswith(entry.removesuffix("_") + "<entry:")
            assert session.list_runnables("runnable")["default"] == setting
        else:
            assert session.list_runnables("runnable")["default"] == entry
            assert setting == entry
        thread_id = session.create_thread()
        absolute_workspace = core.layout.home / "lab" / "absolute"
        absolute_workspace.mkdir(parents=True)
        assert session.resolve_workdir(None, None, thread_id) == "lab://"
        assert session.resolve_workdir(str(absolute_workspace), None, thread_id) == (
            "lab://absolute"
        )
        request = session.build_request(
            thread_id,
            RunOverride(runnable=entry),
            CallInput({"_": "hello"}),
            session.initial_setting(),
        )
        session.run(
            request,
            events.append,
            errors.append,
            states.append,
        )
        assert session.resolve_workdir(None, None, thread_id) == "lab://"
        result = session.get_result(None, thread_id=thread_id)

        assert isinstance(events[0], RunBegin)
        assert isinstance(events[-1], RunEnd)
        root_id = events[0].run
        assert states == [
            RunAccepted(root_id),
            RunWorkdirUpdated(root_id, "lab://"),
        ]
        assert result.run_id == root_id
        assert result.output == (TextPart("remote response"),)
        assert errors == []
    finally:
        session.close()
        asyncio.run(core.close())


def test_remote_chat_default_runnable_tracks_the_latest_state(tmp_path: Path) -> None:
    original = ExecutionHarness.create(
        tmp_path,
        source="agic chat:\n  hello\n",
        responses=(),
        prepare_state=True,
    )
    original.store.close()
    configured_setup = replace(
        original.setup,
        defaults=replace(original.setup.defaults, runnable="agic:chat"),
    )
    state_snapshot = _Snapshot(original.state)
    core = AgentCore(original.setup.layout)
    core.setup = _Snapshot(configured_setup)
    core.state = state_snapshot
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

    try:
        assert session.initial_setting().runnable == "agic:chat"
        (original.setup.layout.home / "agent.too").write_text(
            "agic assistant:\n  hello\n",
            encoding="utf-8",
        )
        state_snapshot.value = prepare_agent_state(original.setup.layout)
        request = session.build_request(
            session.create_thread(),
            RunOverride(),
            CallInput({"_": "hello"}),
            session.initial_setting(),
        )
        assert request.runnable.ref == "agic:assistant"
    finally:
        session.close()
        asyncio.run(core.close())


@pytest.mark.parametrize("remote", [False, True], ids=["local", "remote"])
@pytest.mark.parametrize("operation", ["run", "exec"])
def test_chat_named_invocation_keeps_the_next_turn_on_the_session_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: bool, operation: str
) -> None:
    from toolang.base.types.run import ToolCall
    from toolang.cli.toolang.commands.chat import local
    from tests.support.execution_assertions import (
        last_tool_result,
        route_snapshots,
        without_runtime_snapshots,
    )

    target = "flow:abc" if operation == "exec" else "agic:xyz"
    source = """
agic chat(_: Text) -> Text:
  recall = none
  context = none
  user: {{_}}

flow abc(_: Text) -> Text:
  pass

agic xyz(_: Text) -> Text:
  recall = none
  context = none
  user: {{_}}
"""
    responses = [
        ModelCallResult(
            tool_calls=(
                ToolCall(
                    "named",
                    "named",
                    f"_toolang__{operation}",
                    {"runnable": target, "input": {"_": "payload"}},
                ),
            )
        )
    ]
    if operation == "run":
        responses.extend(
            [
                ModelCallResult(message=Message.assistant("target result")),
                ModelCallResult(message=Message.assistant("Summary: target result")),
            ]
        )
    responses.append(ModelCallResult(message=Message.assistant("chat again")))
    harness = ExecutionHarness.create(tmp_path, source=source, responses=responses)
    harness.store.close()
    core = None
    if remote:
        core = AgentCore(harness.setup.layout)
        core.setup = _Snapshot(harness.setup)
        core.state = _Snapshot(harness.state)
        agents.write_runtime_state(
            core.layout,
            endpoint="http://runtime.test:7001",
            started_at="2026-10-02T00:00:00Z",
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
    else:

        class Watcher(_Snapshot):
            async def sync(self):
                from toolang.state.types import StateSyncResult

                return StateSyncResult(harness.state.revision, harness.state.files)

            async def refresh(self) -> object:
                return self.value

            async def run(self, *, stop_signal: asyncio.Event) -> None:
                await stop_signal.wait()

        monkeypatch.setattr(
            local, "SetupWatcher", lambda *_args, **_kwargs: Watcher(harness.setup)
        )
        monkeypatch.setattr(
            local, "StateWatcher", lambda *_args, **_kwargs: Watcher(harness.state)
        )
        session = local.LocalChatSession(harness.setup.layout)

    errors: list[str] = []
    try:
        setting = session.initial_setting()
        assert setting.runnable == "agic:chat"
        thread = session.create_thread()
        first_input = f"Call {target} with payload" + (
            ", then summarize the result." if operation == "run" else "."
        )
        for prompt, expected in [
            (
                first_input,
                "Summary: target result" if operation == "run" else "payload",
            ),
            ("Next question", "chat again"),
        ]:
            request = session.build_request(
                thread, RunOverride(), CallInput({"_": prompt}), setting
            )
            assert request.runnable.ref == "agic:chat"
            session.run(request, lambda _event: None, errors.append)
            assert session.get_result(None, thread_id=thread).output == (
                TextPart(expected),
            )
            assert session.initial_setting().runnable == "agic:chat"
        assert errors == []
        calls = [invocation.call for invocation in harness.adapter.invocations]
        assert len(calls) == (4 if operation == "run" else 2)
        first = route_snapshots(
            calls[0], requested_only={"hands": True, "handoffs": True}
        )
        assert {entry["ref"] for entry in first["hands"]} == {"flow:abc", "agic:xyz"}
        assert {entry["ref"] for entry in first["handoffs"]} == {
            "agic:chat",
            "flow:abc",
            "agic:xyz",
        }
        assert {"_toolang__run", "_toolang__exec"} <= {
            tool.name for tool in calls[0].tools
        }
        assert without_runtime_snapshots(calls[-1].messages) == [
            Message.user("Next question")
        ]
        if operation == "run":
            assert last_tool_result(calls[2]).error is None
            from toolang.base.types.message import message_text

            assert any(
                'status="succeeded"' in message_text(m.parts)
                and "target result" in message_text(m.parts)
                for m in calls[2].messages
            )
    finally:
        session.close()
        if core is not None:
            asyncio.run(core.close())
