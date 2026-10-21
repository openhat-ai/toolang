"""Coroutine ownership when Chat session submission races with shutdown."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import inspect
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

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
    session._submission_lock = threading.Lock()
    session._close = AsyncMock()
    session.store = SimpleNamespace(close=Mock())
    session._thread = threading.Thread(target=session._run_loop, daemon=True)
    session._thread.start()
    try:
        assert session._ready.wait(timeout=2)
        yield session
    finally:
        try:
            session.close()
        finally:
            if session._thread.is_alive():
                session._loop.call_soon_threadsafe(session._loop.stop)
                session._thread.join(timeout=2)


def test_rejected_shutdown_submission_closes_coroutine(session):
    async def pending():
        pass

    session.close()
    coroutine = pending()
    try:
        with pytest.raises(RuntimeError, match="Event loop is closed"):
            session._submit(coroutine, allow_closed=True)
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    finally:
        coroutine.close()


def test_accepted_submission_finishes_when_shutdown_races_with_scheduling(
    session, monkeypatch
):
    entered = threading.Event()
    release_submit = threading.Event()
    shutdown_checkpoint = threading.Event()
    release_close = threading.Event()
    closing_thread = None
    schedule = asyncio.run_coroutine_threadsafe
    close_loop = session._loop.close
    lock = threading.Lock()

    async def pending():
        await asyncio.Event().wait()

    coroutine = pending()

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread() is closing_thread:
                shutdown_checkpoint.set()
            lock.acquire()

        def __exit__(self, *_args):
            lock.release()

    def wait_for_shutdown(coro, loop):
        if coro is coroutine:
            entered.set()
            assert release_submit.wait(timeout=5)
        return schedule(coro, loop)

    def wait_before_loop_close():
        shutdown_checkpoint.set()
        assert release_close.wait(timeout=5)
        close_loop()

    def shutdown():
        nonlocal closing_thread
        closing_thread = threading.current_thread()
        session.close()

    session._submission_lock = ObservedLock()
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", wait_for_shutdown)
    monkeypatch.setattr(session._loop, "close", wait_before_loop_close)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                submitting = pool.submit(session._submit, coroutine)
                assert entered.wait(timeout=2)
                closing = pool.submit(shutdown)
                assert shutdown_checkpoint.wait(timeout=2)
                release_submit.set()
                submitted = submitting.result(timeout=2)
                release_close.set()
                closing.result(timeout=2)
                assert session._loop.is_closed()
                assert submitted.done()
                assert submitted.cancelled()
                assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
                session._close.assert_awaited_once()
            finally:
                release_submit.set()
                release_close.set()
    finally:
        coroutine.close()


def test_close_stops_loop_and_releases_store_when_cleanup_fails(session):
    failure = RuntimeError("disconnect failed")
    session._close.side_effect = failure

    with pytest.raises(RuntimeError) as caught:
        session.close()

    assert caught.value is failure
    assert not session._thread.is_alive()
    assert session._loop.is_closed()
    if isinstance(session, LocalChatSession):
        session.store.close.assert_called_once()
