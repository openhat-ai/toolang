from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from toolang.base.types.message import TextPart
from toolang.common.errors import ToolangError
from toolang.cli.toolang.commands.chat import slashes
from toolang.cli.toolang.commands.chat.base import ChatResult, QueuedCall
from toolang.cli.toolang.commands.chat.input import QuickCommand
from toolang.cli.toolang.commands.chat.presenter import ChatRunPresenter


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
            "runnable": {
                "default": "agic:chat",
                "items": [
                    {"kind": "agic", "name": "chat"},
                    {"kind": "flow", "name": "review"},
                ],
            },
        }
        self.error: Exception | None = None
        self.results = {
            "run_saved": ChatResult(
                run_id="run_saved",
                output=(TextPart("saved result"),),
            )
        }

    def list_models(self) -> Mapping[str, Any]:
        if self.error is not None:
            raise self.error
        return self.models

    def list_executables(self, kind: str) -> Mapping[str, Any]:
        if self.error is not None:
            raise self.error
        return self.executables[kind]

    def get_result(
        self,
        run_id: str | None,
        *,
        thread_id: str | None,
    ) -> ChatResult:
        del thread_id
        return self.results[run_id or "run_saved"]


class _App:
    def __init__(self) -> None:
        self.client = _Client()
        self.selects: dict[str, object] = {}
        self.queue: list[QueuedCall] = []
        self.active_run: str | None = None
        self.error = ""
        self.replaced = ""
        self.steers: list[str] = []
        self.exited = False
        self.status_refreshes = 0
        self.live_blocks: list[Any] = []
        self.presenter = ChatRunPresenter()
        self.thread_id: str | None = "thread_1"

    def get_client(self) -> Any:
        return self.client

    def get_selects(self) -> dict[str, object]:
        return self.selects

    def get_queue(self) -> list[QueuedCall]:
        return self.queue

    def get_active_run(self) -> str | None:
        return self.active_run

    def get_thread_id(self) -> str | None:
        return self.thread_id

    def set_active_run(self, run_id: str | None) -> None:
        self.active_run = run_id

    def get_live_blocks(self) -> Any:
        return self.live_blocks

    def get_presenter(self) -> ChatRunPresenter:
        return self.presenter

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


def test_quick_handle_reports_unknown_commands() -> None:
    app = _App()

    result = slashes.handle(app, QuickCommand("missing"))

    assert result.handled is True
    assert app.error == "Unknown command: :missing"


def test_quick_help_and_exit_are_declarative_commands() -> None:
    app = _App()

    help_result = slashes.handle(app, QuickCommand("?"))
    exit_result = slashes.handle(app, QuickCommand("quit"))

    assert help_result.lines is not None
    assert help_result.lines[0] == "Chat Commands"
    assert exit_result.handled is True
    assert app.exited is True


def test_quick_model_lists_models_without_changing_settings() -> None:
    app = _App()

    listed = slashes.handle(app, QuickCommand("model"))

    assert listed.lines == ["Available Models", "[openai]  default  openai"]
    assert app.selects == {}
    assert app.status_refreshes == 0


def test_quick_executable_lists_without_changing_settings() -> None:
    app = _App()
    app.selects["flow"] = "review"

    result = slashes.handle(app, QuickCommand("agic"))

    assert result.lines == ["Available Agics", "chat  default"]
    assert app.selects == {"flow": "review"}
    assert app.status_refreshes == 0


def test_quick_runnable_lists_qualified_agics_and_flows() -> None:
    app = _App()
    app.selects["flow"] = "review"

    result = slashes.handle(app, QuickCommand("runnable"))

    assert result.lines == [
        "Available Runnables",
        "agic:chat  default",
        "flow:review  current",
    ]


def test_quick_show_loads_an_explicit_or_latest_durable_result() -> None:
    app = _App()

    explicit = slashes.handle(app, QuickCommand("show", "run_saved"))
    latest = slashes.handle(app, QuickCommand("show"))

    assert explicit.result == ChatResult(
        run_id="run_saved",
        output=(TextPart("saved result"),),
    )
    assert latest.result == explicit.result


def test_quick_queue_edits_and_steers_numbered_items() -> None:
    app = _App()
    app.queue[:] = [QueuedCall("first", {}), QueuedCall("second", {})]

    slashes.handle(app, QuickCommand("queue", "edit 2"))
    app.active_run = "run_abc"
    slashes.handle(app, QuickCommand("queue", "steer 1"))

    assert app.replaced == "second"
    assert app.steers == ["first"]
    assert app.queue == []


def test_quick_client_errors_are_reported_in_status() -> None:
    app = _App()
    app.client.error = ToolangError("unavailable")

    result = slashes.handle(app, QuickCommand("model"))

    assert result.handled is True
    assert result.lines is None
    assert app.error == "unavailable"


def test_quick_steer_requires_message() -> None:
    app = _App()

    slashes.handle(app, QuickCommand("steer"))

    assert app.error == ":steer requires a message."
