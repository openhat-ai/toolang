from __future__ import annotations

import asyncio
from pathlib import Path
import threading

import pytest

from toolang.common.layout import AgentLayout
from toolang.state import watcher as state_watcher
from toolang.state.prepare import prepare_agent_state


def test_current_requires_initial_refresh(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path / "toolang", "alice")
    watcher = state_watcher.StateWatcher(layout)

    with pytest.raises(RuntimeError, match="has not been refreshed"):
        watcher.current()


def test_timeout_check_recovers_change_before_watch_registration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def run() -> None:
        toolang_root = tmp_path / "toolang"
        home = toolang_root / "agents" / "alice"
        home.mkdir(parents=True)
        program = home / "agent.too"
        program.write_text("agent alice\n", encoding="utf-8")
        layout = AgentLayout.resident(toolang_root, "alice")
        watcher = state_watcher.StateWatcher(layout)
        initial = await watcher.refresh()
        program.write_text(
            "agent alice\n\nagic chat:\n  Registered late.\n",
            encoding="utf-8",
        )

        async def one_timeout(*_args, **_kwargs):
            yield set()

        monkeypatch.setattr(state_watcher, "awatch", one_timeout)
        updates = watcher.updates(stop_signal=asyncio.Event())
        changed = await anext(updates)

        assert changed.revision != initial.revision
        assert (
            changed.modules["agent"].agics[0].messages[0].content == "Registered late."
        )

    asyncio.run(run())


def test_timeout_check_skips_full_prepare_when_metadata_is_current(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def run() -> None:
        toolang_root = tmp_path / "toolang"
        home = toolang_root / "agents" / "alice"
        home.mkdir(parents=True)
        (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
        layout = AgentLayout.resident(toolang_root, "alice")
        watcher = state_watcher.StateWatcher(layout)
        await watcher.refresh()

        async def one_timeout(*_args, **_kwargs):
            yield set()

        monkeypatch.setattr(state_watcher, "awatch", one_timeout)

        def fail_prepare(*_args, **_kwargs):
            raise AssertionError("unchanged timeout must not load full Agent State")

        monkeypatch.setattr(state_watcher, "prepare_agent_state", fail_prepare)

        observed = [
            state async for state in watcher.updates(stop_signal=asyncio.Event())
        ]
        assert observed == []

    asyncio.run(run())


def test_current_publication_does_not_retrigger_candidate_preparation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def run() -> None:
        toolang_root = tmp_path / "toolang"
        home = toolang_root / "agents" / "alice"
        home.mkdir(parents=True)
        program = home / "agent.too"
        program.write_text("agent alice\n", encoding="utf-8")
        layout = AgentLayout.resident(toolang_root, "alice")
        watcher = state_watcher.StateWatcher(layout)
        await watcher.refresh()
        program.write_text(
            "agent alice\n\nagic chat:\n  Changed.\n",
            encoding="utf-8",
        )
        calls = 0
        prepare = state_watcher.prepare_agent_state

        def counted_prepare(*args, **kwargs):
            nonlocal calls
            calls += 1
            return prepare(*args, **kwargs)

        async def source_then_current(*_args, **_kwargs):
            yield {(state_watcher.Change.modified, str(program))}
            yield {
                (
                    state_watcher.Change.modified,
                    str(state_watcher.agent_current_path(layout)),
                )
            }

        monkeypatch.setattr(state_watcher, "prepare_agent_state", counted_prepare)
        monkeypatch.setattr(state_watcher, "awatch", source_then_current)

        observed = [
            state async for state in watcher.updates(stop_signal=asyncio.Event())
        ]

        assert len(observed) == 1
        assert calls == 1

    asyncio.run(run())


def test_rejected_candidate_does_not_retry_partially_published_layers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def run() -> None:
        toolang_root = tmp_path / "toolang"
        root_prompt = toolang_root / "prompts" / "review.md"
        root_prompt.parent.mkdir(parents=True)
        root_prompt.write_text("Review once.\n", encoding="utf-8")
        home = toolang_root / "agents" / "alice"
        flows = home / "flows"
        flows.mkdir(parents=True)
        (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
        flow = flows / "research.too"
        flow.write_text("flow research:\n  pass\n", encoding="utf-8")
        layout = AgentLayout.resident(toolang_root, "alice")
        watcher = state_watcher.StateWatcher(layout)
        initial = await watcher.refresh()

        root_prompt.write_text("Review twice.\n", encoding="utf-8")
        flow.write_text("flow other:\n  pass\n", encoding="utf-8")
        calls = 0
        prepare = state_watcher.prepare_agent_state

        def counted_prepare(*args, **kwargs):
            nonlocal calls
            calls += 1
            return prepare(*args, **kwargs)

        async def rejected_then_timeout(*_args, **_kwargs):
            yield {
                (state_watcher.Change.modified, str(root_prompt)),
                (state_watcher.Change.modified, str(flow)),
            }
            yield set()

        monkeypatch.setattr(state_watcher, "prepare_agent_state", counted_prepare)
        monkeypatch.setattr(state_watcher, "awatch", rejected_then_timeout)

        observed = [
            state async for state in watcher.updates(stop_signal=asyncio.Event())
        ]

        assert observed == []
        assert watcher.current() is initial
        assert calls == 1
        assert watcher.diagnostics()[0].code == "invalid-flow-export"

    asyncio.run(run())


def test_concurrent_refresh_requests_each_run_one_serialized_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def run() -> None:
        toolang_root = tmp_path / "toolang"
        home = toolang_root / "agents" / "alice"
        home.mkdir(parents=True)
        (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
        layout = AgentLayout.resident(toolang_root, "alice")
        watcher = state_watcher.StateWatcher(layout)
        initial = await watcher.refresh()
        calls = 0

        def counted_prepare(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return initial

        monkeypatch.setattr(state_watcher, "prepare_agent_state", counted_prepare)

        first, second = await asyncio.gather(watcher.refresh(), watcher.refresh())

        assert calls == 2
        assert first is initial
        assert second is initial

    asyncio.run(run())


def test_canceling_refresh_does_not_cancel_its_owned_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def run() -> None:
        toolang_root = tmp_path / "toolang"
        home = toolang_root / "agents" / "alice"
        home.mkdir(parents=True)
        program = home / "agent.too"
        program.write_text("agent alice\n", encoding="utf-8")
        watcher = state_watcher.StateWatcher(
            AgentLayout.resident(toolang_root, "alice")
        )
        initial = await watcher.refresh()
        program.write_text(
            "agent alice\n\nagic chat:\n  Changed.\n",
            encoding="utf-8",
        )
        started = threading.Event()
        release = threading.Event()
        prepare = state_watcher.prepare_agent_state

        def delayed_prepare(*args, **kwargs):
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release State preparation")
            return prepare(*args, **kwargs)

        monkeypatch.setattr(state_watcher, "prepare_agent_state", delayed_prepare)
        refresh = asyncio.create_task(watcher.refresh())
        assert await asyncio.to_thread(started.wait, 5)
        check = watcher._check_task
        assert check is not None

        refresh.cancel()
        with pytest.raises(asyncio.CancelledError):
            await refresh
        release.set()
        await check

        assert watcher.current().revision != initial.revision

    asyncio.run(run())


def test_only_one_filesystem_monitor_can_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def run() -> None:
        toolang_root = tmp_path / "toolang"
        home = toolang_root / "agents" / "alice"
        home.mkdir(parents=True)
        (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
        watcher = state_watcher.StateWatcher(
            AgentLayout.resident(toolang_root, "alice")
        )
        await watcher.refresh()
        started = asyncio.Event()
        release = asyncio.Event()

        async def waiting_watch(*_args, **_kwargs):
            started.set()
            await release.wait()
            if False:  # pragma: no cover - keeps this an async generator
                yield set()

        monkeypatch.setattr(state_watcher, "awatch", waiting_watch)

        async def consume() -> list[object]:
            return [
                state async for state in watcher.updates(stop_signal=asyncio.Event())
            ]

        first = asyncio.create_task(consume())
        await started.wait()
        second = watcher.updates(stop_signal=asyncio.Event())
        with pytest.raises(RuntimeError, match="already monitoring"):
            await anext(second)
        release.set()

        assert await first == []

    asyncio.run(run())


def test_invalid_flow_candidate_retains_last_valid_state_until_repaired(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        toolang_root = tmp_path / "toolang"
        home = toolang_root / "agents" / "alice"
        flows = home / "flows"
        flows.mkdir(parents=True)
        (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
        flow = flows / "research.too"
        flow.write_text("flow research:\n  pass\n", encoding="utf-8")
        watcher = state_watcher.StateWatcher(
            AgentLayout.resident(toolang_root, "alice")
        )
        initial = await watcher.refresh()

        flow.write_text("flow other:\n  pass\n", encoding="utf-8")
        refresh = await watcher.refresh_result()
        rejected = refresh.state

        assert rejected is initial
        assert rejected.revision == initial.revision
        assert refresh.diagnostics == watcher.diagnostics()
        assert watcher.diagnostics()[0].layer == "flow-extension"
        assert watcher.diagnostics()[0].authored_path == "flows/research.too"

        flow.write_text("flow research:\n  pass\n", encoding="utf-8")
        repaired = await watcher.refresh()

        assert repaired.revision != initial.revision
        assert "research" in repaired.runnables
        assert watcher.diagnostics() == ()

    asyncio.run(run())


def test_watcher_bootstraps_last_good_state_before_rejecting_current_source(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    flows = home / "flows"
    flows.mkdir(parents=True)
    (home / "agent.too").write_text("agent alice\n", encoding="utf-8")
    flow = flows / "research.too"
    flow.write_text("flow research:\n  pass\n", encoding="utf-8")
    layout = AgentLayout.resident(toolang_root, "alice")
    previous = prepare_agent_state(layout)
    flow.write_text("flow other:\n  pass\n", encoding="utf-8")

    watcher = state_watcher.StateWatcher(layout)
    rejected = asyncio.run(watcher.refresh())

    assert rejected.revision == previous.revision
    assert watcher.diagnostics()[0].code == "invalid-flow-export"


def test_watcher_loads_an_older_persisted_state_after_publishing_a_new_one(
    tmp_path: Path,
) -> None:
    toolang_root = tmp_path / "toolang"
    home = toolang_root / "agents" / "alice"
    home.mkdir(parents=True)
    program = home / "agent.too"
    program.write_text("agic answer:\n  First.\n", encoding="utf-8")
    layout = AgentLayout.resident(toolang_root, "alice")
    first = prepare_agent_state(layout)
    program.write_text("agic answer:\n  Second.\n", encoding="utf-8")
    second = prepare_agent_state(layout)

    watcher = state_watcher.StateWatcher(layout)
    loaded = watcher.load(first.revision)

    assert watcher.current().revision == second.revision
    assert loaded.revision == first.revision
    assert loaded.modules["agent"].agics[0].messages[0].content == "First."
