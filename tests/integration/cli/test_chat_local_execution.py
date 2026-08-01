"""Process-local chat integration with canonical execution events."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import threading
from typing import Any

from anyio import to_process

from tests.support.execution_harness import ExecutionHarness
from toolang.base.types.message import Message, TextPart
from toolang.base.types.run import ModelCallResult
from toolang.cli.toolang.commands.chat import local
from toolang.execution.events import RunEvent


def test_local_chat_owned_loop_drains_detached_tasks_before_close() -> None:
    loop = asyncio.new_event_loop()
    cleaned = False

    async def detached_task() -> None:
        nonlocal cleaned
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned = True

    task = loop.create_task(detached_task())
    loop.run_until_complete(asyncio.sleep(0))

    local._close_event_loop(loop)

    assert task.cancelled()
    assert cleaned
    assert loop.is_closed()


def test_local_chat_owned_loop_drains_anyio_process_pool_shutdown_task() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        worker_pid = loop.run_until_complete(to_process.run_sync(os.getpid))
        pending = asyncio.all_tasks(loop)

        assert worker_pid != os.getpid()
        assert {task.get_name() for task in pending} == {
            "AnyIO process pool shutdown task"
        }

        local._close_event_loop(loop)

        assert all(task.done() for task in pending)
        assert loop.is_closed()
    finally:
        asyncio.set_event_loop(None)


def test_local_chat_close_cancels_watchers_without_waiting_for_polling() -> None:
    async def scenario() -> None:
        session: Any = object.__new__(local.LocalChatSession)
        session._stop_signal = asyncio.Event()

        class Executor:
            async def shutdown(self) -> None:
                pass

        session.executor = Executor()

        async def wait_forever() -> None:
            await asyncio.Event().wait()

        session._watch_tasks = (
            asyncio.create_task(wait_forever()),
            asyncio.create_task(wait_forever()),
        )
        await asyncio.sleep(0)

        await asyncio.wait_for(session._close(), timeout=0.1)

        assert session._stop_signal.is_set()
        assert all(task.cancelled() for task in session._watch_tasks)

    asyncio.run(scenario())


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
        assert session.get_result(None, thread_id=thread_id).output == (
            TextPart("hello back"),
        )
        assert session.get_result(runs[0].id, thread_id=None).output == (
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
