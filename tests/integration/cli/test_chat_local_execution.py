"""Process-local chat integration with canonical execution events."""

from __future__ import annotations

import asyncio
from pathlib import Path
import threading
from typing import Any

from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.cli.toolang.commands.chat import local
from toolang.execution.events import RunEvent


def test_local_chat_uses_run_executor_and_canonical_tracer(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic chat(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[ModelCallResult(message=Message.assistant("hello back"))],
    )
    harness.store.close()

    class SetupWatcher:
        def __init__(self, _layout: object) -> None:
            pass

        def current(self):
            return harness.setup

        async def refresh(self, *, force: bool = False):
            del force
            return harness.setup

        async def run(self, *, stop_signal: asyncio.Event) -> None:
            await stop_signal.wait()

    class StateWatcher:
        def __init__(self, _layout: object) -> None:
            self.state = harness.state

        def current(self):
            return self.state

        async def refresh(self, *, force: bool = False):
            del force
            return self.state

        async def run(self, *, stop_signal: asyncio.Event) -> None:
            await stop_signal.wait()

    monkeypatch.setattr(local, "SetupWatcher", SetupWatcher)
    monkeypatch.setattr(local, "StateWatcher", StateWatcher)

    session = local.LocalChatSession(
        harness.setup.layout,
    )
    events: list[RunEvent] = []
    event_threads: list[int] = []
    errors: list[str] = []

    def on_event(event: RunEvent) -> None:
        events.append(event)
        event_threads.append(threading.get_ident())

    try:
        thread_id = session.create_thread()
        session.start_run(
            thread_id,
            "hello",
            {},
            on_event,
            errors.append,
        )

        runs = session.store.list_runs(thread_id=thread_id, limit=None)
        assert errors == []
        assert [event.type for event in events] == [
            "run_begin",
            "step_begin",
            "part_begin",
            "part_end",
            "step_end",
            "run_end",
        ]
        assert len(runs) == 1
        assert runs[0].status == "finished"
        assert session.store.run_output(run_id=runs[0].id) == (
            TextPart("hello back"),
        )
        assert harness.adapter.invocations[0].call.messages == [
            Message.user("hello")
        ]
        assert set(event_threads) == {session._thread.ident}
        assert threading.get_ident() != session._thread.ident
    finally:
        session.close()


def test_chat_session_does_not_create_a_thread_on_open(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat:\n  hello\n",
        responses=[],
    )
    harness.store.close()

    class SetupWatcher:
        def __init__(self, _layout: object) -> None:
            pass

        def current(self):
            return harness.setup

        async def refresh(self, *, force: bool = False):
            del force
            return harness.setup

        async def run(self, *, stop_signal: asyncio.Event) -> None:
            await stop_signal.wait()

    class StateWatcher:
        def __init__(self, _layout: object) -> None:
            self.state = harness.state

        def current(self):
            return self.state

        async def refresh(self, *, force: bool = False):
            del force
            return self.state

        async def run(self, *, stop_signal: asyncio.Event) -> None:
            await stop_signal.wait()

    monkeypatch.setattr(local, "SetupWatcher", SetupWatcher)
    monkeypatch.setattr(local, "StateWatcher", StateWatcher)

    session = local.LocalChatSession(
        harness.setup.layout,
    )
    try:
        assert session.store.list_threads() == []
    finally:
        session.close()
