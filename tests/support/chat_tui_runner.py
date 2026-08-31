"""Shared subprocess setup for chat TUI system tests."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace

from toolang.base.types.model import ModelRequest
from toolang.cli.toolang.commands.chat import local
from toolang.cli.toolang.commands.chat.tui import ChatTuiApp
from toolang.plugin.models.collections import ModelCollection
from toolang.setup import AgentSetup
from toolang.state.state import AgentState, StatePublication, publish_state_resources
from toolang.state.watcher import StateRefresh


def run_chat_tui(
    setup: AgentSetup,
    state: AgentState,
    *,
    selects: Mapping[str, object],
    models: Sequence[str] = (),
) -> None:
    """Run a local chat TUI with fixed setup and state snapshots."""

    if models:
        setup = replace(
            setup,
            models=ModelCollection(tuple(setup.models.resolve(ref) for ref in models)),
        )
    publication = publish_state_resources(state, agent_name=setup.layout.name)

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
        def __init__(self, _layout: object, **_kwargs: object) -> None:
            pass

        def current(self) -> StatePublication:
            return publication

        async def refresh(self, *, force: bool = False) -> StatePublication:
            del force
            return publication

        async def refresh_result(self, *, force: bool = False) -> StateRefresh:
            del force
            return StateRefresh(publication)

        async def run(self, *, stop_signal: asyncio.Event) -> None:
            await stop_signal.wait()

    local.SetupWatcher = SetupWatcher  # type: ignore[invalid-assignment]
    local.StateWatcher = StateWatcher  # type: ignore[invalid-assignment]
    session = local.LocalChatSession(setup.layout)
    try:
        setting = session.initial_setting()
        selected_model = selects.get("model")
        if isinstance(selected_model, str):
            setting = replace(setting, model=ModelRequest(selected_model))
        for kind in ("flow", "agic"):
            selected_runnable = selects.get(kind)
            if isinstance(selected_runnable, str):
                setting = replace(
                    setting,
                    runnable=f"{kind}:{selected_runnable}",
                )
                break
        ChatTuiApp.run(
            thread_id=None,
            setting=setting,
            home=str(setup.layout.home),
            input_history=None,
            client=session,
        )
    finally:
        session.close()
