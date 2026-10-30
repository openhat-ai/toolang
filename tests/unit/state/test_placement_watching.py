"""Every placement observes external State publication through the same layout."""

from pathlib import Path
import asyncio
from typing import Literal

import pytest

from toolang.common import watching
from toolang.common.layout import AgentLayout
from toolang.state import watcher as watcher_module
from toolang.state.cache import (
    AGENT_STATE_FORMAT,
    agent_current_path,
    layer_current_path,
)
from toolang.state.prepare import prepare_agent_state, load_agent_state
from toolang.state.watcher import StateWatcher
from toolang.up.source import materialize_roaming_program


def test_state_watch_directories_exclude_generated_trees(tmp_path: Path):
    layout = AgentLayout.resident(tmp_path, "a")
    layout.agent_home.mkdir(parents=True)
    nested = layout.agent_home / "skills/pdf/references"
    nested.mkdir(parents=True)
    version = f"v{AGENT_STATE_FORMAT}"
    revisions = layout.agent_state / version / "revs"
    for directory in (
        layout.runtime / "logs/noisy",
        layout.lab / "nested",
        revisions / "revision/source",
    ):
        directory.mkdir(parents=True)
    watched = watcher_module._watch_directories(layout)
    assert nested in watched
    assert layout.agent_home in watched
    assert layout.agent_state / version in watched
    assert all(not path.is_relative_to(layout.runtime) for path in watched)
    assert all(not path.is_relative_to(layout.workspaces) for path in watched)
    assert all(not path.is_relative_to(revisions) for path in watched)


@pytest.mark.parametrize("placement", ["resident", "visiting", "roaming"])
@pytest.mark.parametrize("scope", ["root", "home", "agent"])
def test_external_current_change_is_checked_without_source_edits(
    tmp_path: Path, monkeypatch, placement: str, scope: Literal["root", "home", "agent"]
):
    if placement == "roaming":
        source = tmp_path / "a.too"
        source.write_text("flow work():\n  pass\n")
        layout = materialize_roaming_program(source, toolang_root=tmp_path / "root")
    else:
        layout = (
            AgentLayout.resident(tmp_path, "a")
            if placement == "resident"
            else AgentLayout.visiting("https://example.test/a.too", "a", root=tmp_path)
        )
        layout.agent_home.mkdir(parents=True)
        layout.program.write_text("flow work():\n  pass\n")
    initial = prepare_agent_state(layout)
    pointer = (
        agent_current_path(layout)
        if scope == "agent"
        else layer_current_path(layout, scope)
    )
    original_watch = watching.awatch
    original_prepare = watcher_module.prepare_agent_state

    async def scenario():
        registered = asyncio.Event()
        checked = asyncio.Event()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def watch(*args, **kwargs):
            assert kwargs["recursive"] is False
            registered.set()
            async for paths in original_watch(*args, **kwargs):
                yield paths

        def prepare(*args, **kwargs):
            state = original_prepare(*args, **kwargs)
            loop.call_soon_threadsafe(checked.set)
            return state

        monkeypatch.setattr(watching, "awatch", watch)
        monkeypatch.setattr(watcher_module, "prepare_agent_state", prepare)
        observer = StateWatcher(layout, initial_state=initial)
        task = asyncio.create_task(
            observer.run(stop_signal=stop, interval_ms=50, debounce_ms=50)
        )
        try:
            async with asyncio.timeout(10):
                await registered.wait()
                pointer.write_text("f" * 64 + "\n")
                await checked.wait()
            assert pointer.read_text().strip() != "f" * 64
            assert set(load_agent_state(layout).runnables) == {"work"}
            assert layout.program.read_text() == "flow work():\n  pass\n"
        finally:
            stop.set()
            await task

    asyncio.run(scenario())
