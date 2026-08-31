from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from toolang.base.types.message import TextPart
from toolang.base.types.model import ModelParameters, ModelRequest, ReasoningParameters
from toolang.base.types.policy import RunPolicy
from toolang.common.errors import ToolangError
from toolang.execution.policy import apply_session_setting
from toolang.execution.schemas import RunRequest, RunnableRequest
from toolang.execution.types import RunOverride, SessionSetting
from toolang.lang.input import RunnableInputRaw
from toolang.cli.toolang.commands.chat import slashes
from toolang.cli.toolang.commands.chat.base import ChatResult, QueuedCall
from toolang.cli.toolang.commands.chat.input import QuickCommand
from toolang.cli.toolang.commands.chat.presenter import ChatRunPresenter


class _Client:
    def __init__(self) -> None:
        self.models: Mapping[str, Any] = {
            "default": "openai/gpt-5",
            "items": [
                {
                    "ref": "openai/gpt-5",
                    "name": "GPT-5",
                    "provider": "openai",
                    "parameters": {"reasoning": {"effort": ["low", "high", "default"]}},
                }
            ],
        }
        self.runnables: dict[str, Mapping[str, Any]] = {
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
        self.applied: list[RunOverride] = []

    def list_models(self) -> Mapping[str, Any]:
        if self.error is not None:
            raise self.error
        return self.models

    def list_runnables(self, kind: str) -> Mapping[str, Any]:
        if self.error is not None:
            raise self.error
        return self.runnables[kind]

    def apply_setting(
        self,
        setting: SessionSetting,
        update: RunOverride,
    ) -> SessionSetting:
        self.applied.append(update)
        return apply_session_setting(_surface(), setting, update)

    def get_result(
        self,
        run_id: str | None,
        *,
        thread_id: str | None,
    ) -> ChatResult:
        del thread_id
        return ChatResult(
            run_id=run_id or "run_saved",
            output=(TextPart("saved result"),),
        )


def _surface() -> SessionSetting:
    return SessionSetting(
        model=ModelRequest("openai/gpt-5"),
        runnable="agic:chat",
    )


def _request(source: str) -> RunRequest:
    return RunRequest(
        thread_id="thread_1",
        request_id=f"request_{source}",
        runnable=RunnableRequest("agic:chat", RunnableInputRaw(_=source)),
        model=ModelRequest("openai/gpt-5"),
        policy=RunPolicy(),
    )


class _App:
    def __init__(self) -> None:
        self.client = _Client()
        self.setting = _surface()
        self.queue: list[QueuedCall] = []
        self.active_run: str | None = None
        self.error = ""
        self.replaced = ""
        self.steers: list[str] = []
        self.exited = False
        self.status_refreshes = 0
        self.live_blocks: list[Any] = []
        self.presenter = ChatRunPresenter()

    def get_client(self) -> Any:
        return self.client

    def get_setting(self) -> SessionSetting:
        return self.setting

    def set_setting(self, setting: SessionSetting) -> None:
        self.setting = setting

    def get_queue(self) -> list[QueuedCall]:
        return self.queue

    def get_active_run(self) -> str | None:
        return self.active_run

    def get_thread_id(self) -> str | None:
        return "thread_1"

    def set_active_run(self, run_id: str | None) -> None:
        self.active_run = run_id

    def get_live_blocks(self) -> Any:
        return self.live_blocks

    def get_presenter(self) -> ChatRunPresenter:
        return self.presenter

    def ensure_thread_id(self) -> str:
        return "thread_1"

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
    assert app.error == "Unknown command: /missing"


def test_quick_help_and_exit_are_declarative_commands() -> None:
    app = _App()

    help_result = slashes.handle(app, QuickCommand("?"))
    exit_result = slashes.handle(app, QuickCommand("quit"))

    assert help_result.lines is not None
    assert help_result.lines[0] == "Chat Commands"
    assert exit_result.handled is True
    assert app.exited is True


def test_quick_model_requires_a_submitted_setting_without_listing() -> None:
    app = _App()
    app.client.error = AssertionError("model list must not be loaded")

    result = slashes.handle(app, QuickCommand("model"))

    assert result.lines is None
    assert app.error == "/model requires a model or parameter assignment."
    assert app.setting == _surface()
    assert app.status_refreshes == 0


def test_model_identity_and_effort_update_independently() -> None:
    app = _App()

    slashes.handle(app, QuickCommand("model", "openai/gpt-5 effort=high"))
    assert app.setting.model == ModelRequest(
        "openai/gpt-5",
        ModelParameters(ReasoningParameters(effort="high")),
    )

    slashes.handle(app, QuickCommand("model", "effort=low"))
    assert app.setting.model == ModelRequest(
        "openai/gpt-5",
        ModelParameters(ReasoningParameters(effort="low")),
    )

    slashes.handle(app, QuickCommand("model", "effort=4096"))
    assert app.setting.model == ModelRequest(
        "openai/gpt-5",
        ModelParameters(ReasoningParameters(budget_tokens=4096)),
    )

    slashes.handle(app, QuickCommand("model", "effort=auto"))
    assert app.setting.model == ModelRequest("openai/gpt-5")
    assert app.status_refreshes == 4


def test_quick_runnable_requires_an_identity_without_listing() -> None:
    app = _App()
    app.client.error = AssertionError("runnable list must not be loaded")

    result = slashes.handle(app, QuickCommand("agic"))

    assert result.lines is None
    assert app.error == "/agic requires a runnable identity."
    assert app.setting == _surface()


def test_runnable_argument_updates_validated_session_setting() -> None:
    app = _App()

    result = slashes.handle(app, QuickCommand("flow", "review"))

    assert result.lines is None
    assert app.setting.runnable == "flow:review"
    assert app.status_refreshes == 1


def test_allow_and_limit_commands_update_session_setting() -> None:
    app = _App()

    slashes.handle(
        app,
        QuickCommand("allow", "models=openai/* tools=shell/*"),
    )
    slashes.handle(app, QuickCommand("limit", "tokens=2000 time=none"))

    assert app.setting.allow.models == ("openai/*",)
    assert app.setting.allow.tools == ("shell/*",)
    assert app.setting.limits.tokens == 2000
    assert app.setting.limits.time is None


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
    app.queue[:] = [
        QueuedCall("first", _request("first")),
        QueuedCall("second", _request("second")),
    ]

    slashes.handle(app, QuickCommand("queue", "edit 2"))
    app.active_run = "run_abc"
    slashes.handle(app, QuickCommand("queue", "steer 1"))

    assert app.replaced == "second"
    assert app.steers == ["first"]
    assert app.queue == []


def test_quick_client_errors_are_reported_in_status() -> None:
    app = _App()
    app.client.error = ToolangError("unavailable")

    result = slashes.handle(app, QuickCommand("model", "openai/gpt-5"))

    assert result.handled is True
    assert result.lines is None
    assert app.error == "unavailable"


def test_quick_steer_requires_message() -> None:
    app = _App()

    slashes.handle(app, QuickCommand("steer"))

    assert app.error == "/steer requires a message."
