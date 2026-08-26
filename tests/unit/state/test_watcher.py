from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from toolang.common.layout import AgentLayout
from toolang.state import watcher as state_watcher


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

        assert changed.version != initial.version
        assert changed.program.agics[0].messages[0].content == "Registered late."

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

        async def fail_refresh(*, force: bool = False):
            del force
            raise AssertionError("unchanged timeout must not load full prepared state")

        monkeypatch.setattr(watcher, "refresh", fail_refresh)

        observed = [
            state async for state in watcher.updates(stop_signal=asyncio.Event())
        ]
        assert observed == []

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
        rejected = await watcher.refresh()

        assert rejected is initial
        assert rejected.fingerprint == initial.fingerprint
        assert watcher.diagnostics()[0].layer == "flow-extension"
        assert watcher.diagnostics()[0].authored_path == "flows/research.too"

        flow.write_text("flow research:\n  pass\n", encoding="utf-8")
        repaired = await watcher.refresh()

        assert repaired.fingerprint != initial.fingerprint
        assert "research" in repaired.catalog
        assert watcher.diagnostics() == ()

    asyncio.run(run())
