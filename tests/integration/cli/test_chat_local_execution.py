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
from toolang.cli.toolang.commands.chat.base import ChatExecutorMetadata
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

        class RunClient:
            async def close(self) -> None:
                pass

        session.run_client = RunClient()

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


def test_local_chat_run_request_keeps_chat_fallback_agic_only() -> None:
    requests = []

    class Handle:
        run_id = "run_test"

        async def wait(self) -> None:
            pass

    class RunClient:
        async def start(self, request, *, tracer=None):
            del tracer
            requests.append(request)
            return Handle()

    async def scenario() -> None:
        session: Any = object.__new__(local.LocalChatSession)
        session.run_client = RunClient()

        await session._run("term_test", "hello", {}, lambda _event: None)

    asyncio.run(scenario())

    assert len(requests) == 1
    assert requests[0].runnable_fallbacks == ("agic:chat", "default")


def test_local_chat_owner_loop_control_does_not_wait_on_itself() -> None:
    class RunClient:
        async def stop(self, _run_id: str, **_kwargs: object) -> None:
            pass

    class Submitted:
        def __init__(self) -> None:
            self.result_calls = 0
            self.callbacks: list[Any] = []

        def result(self) -> None:
            self.result_calls += 1

        def add_done_callback(self, callback: Any) -> None:
            self.callbacks.append(callback)

    session: Any = object.__new__(local.LocalChatSession)
    session.run_client = RunClient()
    session._thread = threading.current_thread()
    submitted = Submitted()

    def submit(coroutine: Any, *, allow_closed: bool = False) -> Submitted:
        del allow_closed
        coroutine.close()
        return submitted

    session._submit = submit
    errors: list[str] = []

    session.stop_run("run_test", errors.append)

    assert submitted.result_calls == 0
    assert len(submitted.callbacks) == 1
    submitted.callbacks[0](submitted)
    assert submitted.result_calls == 1
    assert errors == []


def test_local_chat_uses_run_client_and_canonical_tracer(
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
        def __init__(self, _layout: object, **_kwargs: object) -> None:
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
    monkeypatch.setattr(
        local,
        "host_sandbox_description",
        lambda: "macOS 27.0 arm64",
    )

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
        assert session.executor_metadata == ChatExecutorMetadata(
            sandbox_selector="host",
            sandbox_detail="macOS 27.0 arm64",
        )
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
        assert runs[0].status == "succeeded"
        assert session.store.run_output(run_id=runs[0].id) == (TextPart("hello back"),)
        assert session.get_result(None, thread_id=thread_id).output == (
            TextPart("hello back"),
        )
        assert session.get_result(runs[0].id, thread_id=None).output == (
            TextPart("hello back"),
        )
        assert harness.adapter.invocations[0].call.messages == [Message.user("hello")]
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
        def __init__(self, _layout: object, **_kwargs: object) -> None:
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
