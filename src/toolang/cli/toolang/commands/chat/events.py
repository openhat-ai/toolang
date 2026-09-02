"""Input and execution events consumed by terminal chat."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from toolang.execution.events import RunEvent

from .base import AppContext, ChatRunState, RunRecovered

ChatUIEventType = Literal[
    "submit",
    "steer",
    "run_event",
    "run_error",
    "run_state",
    "cancel_error",
    "steer_error",
    "interrupt",
    "eof",
    "cancel",
    "clear",
    "quit",
]


@dataclass(frozen=True, slots=True)
class ChatUIEvent:
    """One input or execution event consumed by the chat UI."""

    type: ChatUIEventType
    value: str | RunEvent | ChatRunState | None = None


def handle_run_event(event: RunEvent, app: AppContext) -> None:
    """Apply one ordered native event through the chat presenter."""

    app.get_presenter().handle(event, app)


def handle_run_error(app: AppContext, message: str) -> bool:
    """Finalize an accepted or pending run after a local execution error."""

    return app.get_presenter().handle_error(app, message)


def handle_run_state(state: ChatRunState, app: AppContext) -> bool:
    """Apply one transport lifecycle update through the chat presenter."""

    if isinstance(state, RunRecovered):
        return app.get_presenter().handle_recovered(app, state.detail)
    return False
