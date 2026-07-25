from __future__ import annotations

import asyncio
from pathlib import Path

from toolang.common.layout import AgentLayout
from toolang.state.prepare import prepare_agent_state
from toolang.state import watcher as state_watcher


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
        initial = prepare_agent_state(
            layout,
            toolang_version="0.2.7",
        )
        watcher = state_watcher.StateWatcher(layout, initial)
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
        initial = prepare_agent_state(
            layout,
            toolang_version="0.2.7",
        )
        watcher = state_watcher.StateWatcher(layout, initial)

        async def one_timeout(*_args, **_kwargs):
            yield set()

        monkeypatch.setattr(state_watcher, "awatch", one_timeout)
        monkeypatch.setattr(
            watcher,
            "refresh",
            lambda: (_ for _ in ()).throw(
                AssertionError("unchanged timeout must not load full prepared state")
            ),
        )

        observed = [
            state async for state in watcher.updates(stop_signal=asyncio.Event())
        ]
        assert observed == []

    asyncio.run(run())
