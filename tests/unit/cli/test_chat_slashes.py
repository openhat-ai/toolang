from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from toolang.common.errors import ToolangError
from toolang.cli.toolang.commands.chat import slashes


class _Client:
    def __init__(self) -> None:
        self.models: Mapping[str, Any] = {
            "default": "[openai]",
            "items": [
                {
                    "selector": "[openai]",
                    "ref": "openai/gpt-5",
                    "provider": "openai",
                    "model": "gpt-5",
                }
            ],
        }
        self.executables: dict[str, Mapping[str, Any]] = {
            "agic": {"default": "chat", "items": [{"name": "chat"}]},
            "flow": {"default": None, "items": [{"name": "review"}]},
        }
        self.error: Exception | None = None

    def list_models(self) -> Mapping[str, Any]:
        if self.error is not None:
            raise self.error
        return self.models

    def list_executables(self, kind: str) -> Mapping[str, Any]:
        if self.error is not None:
            raise self.error
        return self.executables[kind]


class _App:
    def __init__(self) -> None:
        self.client = _Client()
        self.selects: dict[str, object] = {}
        self.queue: list[str] = []
        self.active_run: str | None = None
        self.error = ""
        self.replaced = ""
        self.steers: list[str] = []
        self.exited = False
        self.status_refreshes = 0
        self.live_blocks: list[Any] = []

    def get_client(self) -> Any:
        return self.client

    def get_selects(self) -> dict[str, object]:
        return self.selects

    def get_queue(self) -> list[str]:
        return self.queue

    def get_active_run(self) -> str | None:
        return self.active_run

    def set_active_run(self, run_id: str | None) -> None:
        self.active_run = run_id

    def get_live_blocks(self) -> Any:
        return self.live_blocks

    def ensure_thread_id(self) -> str:
        return "tui_test"

    def is_busy(self) -> bool:
        return self.active_run is not None

    def set_status_error(self, message: str) -> None:
        self.error = message

    def refresh_status(self) -> None:
        self.status_refreshes += 1

    def replace_input(self, text: str) -> None:
        self.replaced = text

    def request_steer(self, message: str) -> None:
        self.steers.append(message)

    def request_exit(self) -> None:
        self.exited = True

    def finalize_block(self, block: Any) -> None:
        self.live_blocks.remove(block)

    def finish_run(self) -> None:
        self.active_run = None


def test_slash_handle_distinguishes_messages_and_unknown_commands() -> None:
    app = _App()

    assert slashes.handle(app, "hello").handled is False
    result = slashes.handle(app, "/missing")

    assert result.handled is True
    assert app.error == "Unknown command: /missing"


def test_slash_help_and_exit_are_declarative_commands() -> None:
    app = _App()

    help_result = slashes.handle(app, "/?")
    exit_result = slashes.handle(app, "/quit")

    assert help_result.lines is not None
    assert help_result.lines[0] == "Slash Commands"
    assert exit_result.handled is True
    assert app.exited is True


def test_slash_model_lists_and_updates_selected_model() -> None:
    app = _App()

    listed = slashes.handle(app, "/model")
    selected = slashes.handle(app, "/model openai")

    assert listed.lines == ["Available Models", "[openai]  default  openai"]
    assert selected.lines == ["model: openai/gpt-5"]
    assert app.selects == {"model": "[openai]"}
    assert app.status_refreshes == 1


def test_slash_model_requires_one_unambiguous_model() -> None:
    app = _App()
    app.client.models = {
        "default": "[openai]",
        "items": [
            {
                "selector": "openai/gpt-5[openai]",
                "provider": "openai",
                "model": "gpt-5",
            },
            {
                "selector": "openai/o3[openai]",
                "provider": "openai",
                "model": "o3",
            },
        ],
    }

    ambiguous = slashes.handle(app, "/model openai")
    assert ambiguous.lines is None
    assert app.error == "Model selector must match exactly one model: openai"

    multiple = slashes.handle(app, "/model openai/gpt-5,openai/o3")

    assert multiple.lines is None
    assert app.error == "/model requires exactly one selector."
    assert app.selects == {}


def test_slash_executable_selection_is_mutually_exclusive() -> None:
    app = _App()
    app.selects["flow"] = "review"

    result = slashes.handle(app, "/agic chat")

    assert result.lines == ["agic: chat"]
    assert app.selects == {"agic": "chat"}


def test_slash_queue_edits_and_steers_numbered_items() -> None:
    app = _App()
    app.queue[:] = ["first", "second"]

    slashes.handle(app, "/q e 2")
    app.active_run = "run_abc"
    slashes.handle(app, "/queue steer 1")

    assert app.replaced == "second"
    assert app.steers == ["first"]
    assert app.queue == []


def test_slash_client_errors_are_reported_in_status() -> None:
    app = _App()
    app.client.error = ToolangError("unavailable")

    result = slashes.handle(app, "/model")

    assert result.handled is True
    assert result.lines is None
    assert app.error == "unavailable"


@pytest.mark.parametrize("command", ("/steer", "/s"))
def test_slash_steer_requires_message(command: str) -> None:
    app = _App()

    slashes.handle(app, command)

    assert app.error == "/steer requires a message."
