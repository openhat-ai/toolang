"""Process-local chat integration with canonical execution events."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from pathlib import Path
import threading
from typing import Any

from anyio import to_process
import pytest

from tests.support.execution_harness import (
    ExecutionHarness,
    RecordingTool,
    TEST_MODEL_REF,
)
from toolang.base.types.message import Message, TextPart
from toolang.base.types.model import ModelRequest
from toolang.base.types.policy import AgentCeiling, RunDefaults, RunPolicy
from toolang.base.types.run import ModelCallResult
from toolang.cli.toolang.commands.chat import local
from toolang.cli.toolang.commands.chat.base import ChatExecutorMetadata
from toolang.common.errors import ToolangError
from toolang.execution.events import RunEvent
from toolang.execution.schemas import RunRequest, RunnableRequest
from toolang.execution.types import (
    AllowOverride,
    ModelOverride,
    RunOverride,
    SessionSetting,
)
from toolang.lang.input import RunnableInputRaw
from toolang.state.state import publish_state_resources
from toolang.state.watcher import StateRefresh


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
            async def disconnect(self) -> None:
                pass

        class Executor:
            stopped = False

            async def stop(self) -> None:
                self.stopped = True

        session.run_client = RunClient()
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
        assert session.executor.stopped is True
        assert all(task.cancelled() for task in session._watch_tasks)

    asyncio.run(scenario())


def test_local_chat_run_request_materializes_chat_runnable() -> None:
    requests = []

    class Handle:
        run_id = "run_test"

        async def wait(self) -> None:
            pass

    class RunClient:
        async def run(self, request, *, tracer=None):
            del tracer
            requests.append(request)
            return Handle()

    async def scenario() -> None:
        session: Any = object.__new__(local.LocalChatSession)
        session.run_client = RunClient()
        await session._run(
            RunRequest(
                thread_id="term_test",
                request_id="term_request",
                runspace="coop",
                runnable=RunnableRequest("agic:chat", RunnableInputRaw(_="hello")),
                model=ModelRequest("test/scripted"),
                policy=RunPolicy(allow=(AgentCeiling(models=("test/*",)),)),
            ),
            lambda _event: None,
        )

    asyncio.run(scenario())

    assert len(requests) == 1
    assert requests[0].runnable.ref == "agic:chat"
    assert requests[0].model.ref == "test/scripted"
    assert requests[0].policy.allow == (AgentCeiling(models=("test/*",)),)


def test_local_chat_defaults_materialize_configured_model(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic chat(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=(),
    )
    setup = replace(
        harness.setup,
        defaults=RunDefaults(model=TEST_MODEL_REF, runnable="chat"),
    )
    try:
        defaults = local.LocalChatSession._current_session_setting(
            setup=setup,
            state=harness.state,
        )
    finally:
        harness.store.close()

    assert defaults.model == ModelRequest(TEST_MODEL_REF)
    assert defaults.runnable == "agic:chat"


def test_local_chat_model_list_retains_the_session_default(tmp_path: Path) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="""
agic chat(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=(),
    )
    session: Any = object.__new__(local.LocalChatSession)
    session.setup_watcher = type(
        "SetupWatcher",
        (),
        {"current": lambda _self: harness.setup},
    )()
    session._surface = SessionSetting(
        model=ModelRequest("session/model"),
        runnable="agic:chat",
    )
    try:
        payload = session.list_models()
    finally:
        harness.store.close()

    assert payload["default"] == "session/model"
    assert [item["ref"] for item in payload["items"]] == [TEST_MODEL_REF]


def test_local_chat_queries_resources_and_reconciles_model_ceiling(
    tmp_path: Path,
) -> None:
    harness = ExecutionHarness.create(
        tmp_path,
        source="agic chat:\n  hello\n",
        responses=(),
        tools={
            "test__lookup": RecordingTool(
                "test__lookup",
                output={"ok": True},
            )
        },
    )

    class Snapshot:
        def current(self):
            return harness.setup

    session: Any = object.__new__(local.LocalChatSession)
    session.setup_watcher = Snapshot()
    session._surface = SessionSetting(
        model=ModelRequest(TEST_MODEL_REF),
        runnable="agic:chat",
    )
    try:
        model_items = session.list_models(("test/*",))["items"]
        assert [item["ref"] for item in model_items] == [TEST_MODEL_REF]
        assert model_items[0]["price"] == {"input": None, "output": None}
        assert model_items[0]["parameters"]["reasoning"]["applicable"] is False
        assert session.list_models(("missing/*",))["items"] == []
        assert session.list_models(())["items"] == []
        tool_items = session.list_tools(("test/*",))["items"]
        assert [item["ref"] for item in tool_items] == ["test/test__lookup"]
        assert tool_items[0]["toolset"] == "test"
        assert session.list_tools(())["items"] == []

        disabled = session.apply_setting(
            session.initial_setting(),
            RunOverride(allow=(AllowOverride("models", ()),)),
        )
        assert disabled.model is None
        assert disabled.allow.models == ()

        narrowed = session.apply_setting(
            session.initial_setting(),
            RunOverride(allow=(AllowOverride("models", ("missing/*",)),)),
        )
        assert narrowed.model is None
        assert narrowed.allow.models == ("missing/*",)

        with pytest.raises(
            ValueError,
            match=f"model is outside session allow.models: {TEST_MODEL_REF}",
        ):
            session.apply_setting(
                narrowed,
                RunOverride(model=ModelOverride(identity=TEST_MODEL_REF)),
            )
        assert narrowed.model is None

        current = session.initial_setting()
        with pytest.raises((ToolangError, ValueError), match="unknown"):
            session.apply_setting(
                current,
                RunOverride(allow=(AllowOverride("models", ("*[unknown=true]",)),)),
            )
        assert current == session.initial_setting()
    finally:
        harness.store.close()


def test_local_chat_owner_loop_control_does_not_wait_on_itself() -> None:
    class RunClient:
        async def cancel(self, _run_id: str, **_kwargs: object) -> None:
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

    session.cancel("run_test", errors.append)

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
    publication = publish_state_resources(
        harness.state,
        agent_name=harness.setup.layout.name,
    )
    setup_refreshes = 0
    state_refreshes = 0

    class SetupWatcher:
        def __init__(self, _layout: object, **_kwargs: object) -> None:
            pass

        def current(self):
            return harness.setup

        async def refresh(self, *, force: bool = False):
            nonlocal setup_refreshes
            del force
            setup_refreshes += 1
            return harness.setup

        async def run(self, *, stop_signal: asyncio.Event) -> None:
            await stop_signal.wait()

    class StateWatcher:
        def __init__(self, _layout: object, **_kwargs: object) -> None:
            self.state = publication

        def current(self):
            return self.state

        async def refresh(self, *, force: bool = False):
            nonlocal state_refreshes
            del force
            state_refreshes += 1
            return self.state

        async def refresh_result(self, *, force: bool = False):
            del force
            return StateRefresh(self.state)

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
        assert session.list_runnables("agic") == {
            "default": "chat",
            "items": [{"name": "chat"}, {"name": "default"}],
        }
        assert session.list_prompts(None) == {"items": []}
        thread_id = session.create_thread()
        request = session.build_request(
            thread_id,
            RunOverride(),
            RunnableInputRaw(_="hello"),
            session.initial_setting(),
        )
        session.run(
            request,
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
        assert setup_refreshes == 1
        assert state_refreshes == 1
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
    publication = publish_state_resources(
        harness.state,
        agent_name=harness.setup.layout.name,
    )

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
        def __init__(self, _layout: object, **_kwargs: object) -> None:
            self.state = publication

        def current(self):
            return self.state

        async def refresh(self, *, force: bool = False):
            del force
            return self.state

        async def refresh_result(self, *, force: bool = False):
            del force
            return StateRefresh(self.state)

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
