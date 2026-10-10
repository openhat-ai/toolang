"""Contended State publication locks report waits before preparation starts."""

from concurrent.futures import ThreadPoolExecutor
import fcntl
from threading import Event

import pytest

from toolang.common.layout import AgentLayout
from toolang.state.cache import _agent_check_lock_path, layer_lock_path
from toolang.state.prepare import prepare_agent_state


@pytest.mark.parametrize("scope", ["agent", "root", "home"])
def test_preparation_reports_contended_lock(tmp_path, scope):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("flow run():\n  pass\n")
    path = (
        _agent_check_lock_path(layout)
        if scope == "agent"
        else layer_lock_path(layout, scope)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    waiting = Event()
    events = []

    def observe(event):
        events.append(event)
        if event.label.startswith("Waiting for"):
            waiting.set()

    with path.open("a+b") as held, ThreadPoolExecutor(max_workers=1) as pool:
        fcntl.flock(held, fcntl.LOCK_EX)
        future = pool.submit(prepare_agent_state, layout, progress=observe)
        try:
            assert waiting.wait(2), "State lock blocked without progress"
            assert not future.done()
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
        state = future.result(timeout=10)
    assert state.revision
    wait_event = next(
        event for event in events if event.label.startswith("Waiting for")
    )
    assert [event.status for event in events if event.id == wait_event.id] == [
        "running",
        "ok",
    ]
    events.clear()
    prepare_agent_state(layout, progress=observe)
    assert not any(event.label.startswith("Waiting for") for event in events)
