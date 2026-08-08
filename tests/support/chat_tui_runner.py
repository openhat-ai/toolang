"""Shared subprocess setup for chat TUI system tests."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace

from toolang.base.types.policy import AgentCeiling
from toolang.cli.toolang.commands.chat import local
from toolang.cli.toolang.commands.chat.tui import ChatTuiApp
from toolang.setup import AgentSetup
from toolang.state.state import AgentState


def run_chat_tui(
    setup: AgentSetup,
    state: AgentState,
    *,
    selects: Mapping[str, object],
    models: Sequence[str] = (),
) -> None:
    """Run a local chat TUI with fixed setup and state snapshots."""

    if models:
        setup = replace(setup, ceiling=AgentCeiling(models=tuple(models)))

    class SetupWatcher:
        def __init__(self, _layout: object, **_kwargs: object) -> None:
            pass

        def current(self) -> AgentSetup:
            return setup

        async def refresh(self, *, force: bool = False) -> AgentSetup:
            del force
            return setup

        async def run(self, *, stop_signal: asyncio.Event) -> None:
            await stop_signal.wait()

    class StateWatcher:
        def __init__(self, _layout: object) -> None:
            pass

        def current(self) -> AgentState:
            return state

        async def refresh(self, *, force: bool = False) -> AgentState:
            del force
            return state

        async def run(self, *, stop_signal: asyncio.Event) -> None:
            await stop_signal.wait()

    local.SetupWatcher = SetupWatcher  # type: ignore[invalid-assignment]
    local.StateWatcher = StateWatcher  # type: ignore[invalid-assignment]
    session = local.LocalChatSession(setup.layout)
    try:
        ChatTuiApp.run(
            thread_id=None,
            selects=dict(selects),
            home=str(setup.layout.home),
            input_history=None,
            client=session,
        )
    finally:
        session.close()
