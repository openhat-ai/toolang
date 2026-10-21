"""Coroutine ownership when Chat session submission races with shutdown."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import inspect
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from toolang.cli.toolang.commands.chat.local import LocalChatSession
from toolang.cli.toolang.commands.chat.remote import RemoteChatSession


@pytest.fixture(params=[LocalChatSession, RemoteChatSession], ids=["local", "remote"])
def session(request):
    # Exercise the real loop and shutdown paths without execution or HTTP resources.
    session: Any = object.__new__(request.param)
    session._closed = False
    session._loop = asyncio.new_event_loop()
    session._ready = threading.Event()
    session._close = AsyncMock()
    session.store = SimpleNamespace(close=lambda: None)
    session._thread = threading.Thread(target=session._run_loop, daemon=True)
    session._thread.start()
    try:
        assert session._ready.wait(timeout=2)
        yield session
    finally:
        session.close()


def test_submission_closes_coroutine_when_shutdown_rejects_scheduling(
    session, monkeypatch
):
    entered = threading.Event()
    release = threading.Event()
    caller = threading.current_thread()
    schedule = session._loop.call_soon_threadsafe
    ran = False

    async def pending():
        nonlocal ran
        ran = True

    coroutine = pending()

    def wait_for_shutdown(callback, *args, **kwargs):
        if threading.current_thread() is not caller:
            entered.set()
            assert release.wait(timeout=5)
        return schedule(callback, *args, **kwargs)

    monkeypatch.setattr(session._loop, "call_soon_threadsafe", wait_for_shutdown)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            submitting = pool.submit(session._submit, coroutine)
            try:
                assert entered.wait(timeout=2)
                session.close()
                assert session._loop.is_closed()
            finally:
                release.set()
            with pytest.raises(RuntimeError, match="Event loop is closed"):
                submitting.result(timeout=2)
        assert not ran
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
        session._close.assert_awaited_once()
    finally:
        coroutine.close()
