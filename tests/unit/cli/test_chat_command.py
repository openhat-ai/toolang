"""Terminal chat command orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import click
import pytest

from toolang.base.types.message import TextPart
from toolang.cli.common.output import shorten_home_path
from toolang.cli.toolang.commands.chat import main as chat
from toolang.cli.toolang.commands.chat.base import (
    ChatExecutorMetadata,
    ChatResult,
    ChatRunState,
)
from toolang.common.layout import AgentLayout
from toolang.execution.events import RunEnd, RunEvent, StepEnd
from toolang.execution.types import Local, RunOverride, StepPath
from toolang.up.process import AgentStatus


class _Client:
    executor_metadata = ChatExecutorMetadata()

    def __init__(self) -> None:
        self.created = 0
        self.starts: list[tuple[str, str, dict[str, object]]] = []

    def list_models(self) -> Mapping[str, Any]:
        return {"default": None, "items": []}

    def list_executables(self, kind: str) -> Mapping[str, Any]:
        del kind
        return {"default": None, "items": []}

    def create_thread(self) -> str:
        self.created += 1
        return "term_created"

    def apply_settings(
        self,
        commands: tuple[RunOverride, ...],
        selects: Mapping[str, object],
    ) -> Mapping[str, object]:
        del commands
        return dict(selects)

    def get_result(
        self,
        run_id: str | None,
        *,
        thread_id: str | None,
    ) -> ChatResult:
        del thread_id
        return ChatResult(
            run_id=run_id or "run_latest",
            output=(TextPart("result"),),
        )

    def start_run(
        self,
        thread_id: str,
        message: str,
        selects: Mapping[str, object],
        on_event: Callable[[RunEvent], None],
        on_error: Callable[[str], None],
        on_state: Callable[[ChatRunState], None] | None = None,
    ) -> None:
        del on_event, on_error, on_state
        self.starts.append((thread_id, message, dict(selects)))

    def stop_run(self, run_id: str, on_error: Callable[[str], None]) -> None:
        del run_id, on_error

    def steer_run(
        self,
        run_id: str,
        message: str,
        on_error: Callable[[str], None],
    ) -> None:
        del run_id, message, on_error


class _FailedRunClient(_Client):
    def start_run(
        self,
        thread_id: str,
        message: str,
        selects: Mapping[str, object],
        on_event: Callable[[RunEvent], None],
        on_error: Callable[[str], None],
        on_state: Callable[[ChatRunState], None] | None = None,
    ) -> None:
        del on_error, on_state
        self.starts.append((thread_id, message, dict(selects)))
        on_event(
            RunEnd(
                run="run_failed",
                status="failed",
                error="provider failed",
            )
        )


def test_scripted_chat_exit_does_not_create_an_empty_thread(
    monkeypatch: Any,
) -> None:
    client = _Client()
    inputs = iter((":exit",))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    chat._chat_interactive_scripted_local(
        client=client,
        thread_id=None,
    )

    assert client.created == 0
    assert client.starts == []


def test_scripted_chat_help_does_not_create_an_empty_thread(
    monkeypatch: Any,
) -> None:
    client = _Client()
    inputs = iter((":help", ":exit"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    chat._chat_interactive_scripted_local(
        client=client,
        thread_id=None,
    )

    assert client.created == 0
    assert client.starts == []


def test_scripted_chat_creates_one_thread_for_the_first_submission(
    monkeypatch: Any,
) -> None:
    client = _Client()
    inputs = iter(("hello", "again", ":exit"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    chat._chat_interactive_scripted_local(
        client=client,
        thread_id=None,
        selector_payload={"models": ["test/model"]},
    )

    assert client.created == 1
    assert client.starts == [
        ("term_created", "hello", {"models": ["test/model"]}),
        ("term_created", "again", {"models": ["test/model"]}),
    ]


def test_scripted_chat_reports_a_failed_run(
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _FailedRunClient()
    inputs = iter(("hello", ":exit"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    chat._chat_interactive_scripted_local(
        client=client,
        thread_id="term_existing",
    )

    assert "provider failed" in capsys.readouterr().err


def test_scripted_renderer_uses_model_step_output_without_deltas(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = chat._ScriptedRunRenderer()

    renderer.render(
        StepEnd(
            step=StepPath.parse("run_success.1"),
            kind="model",
            status="succeeded",
            output=Local.typed("Part[]", (TextPart("complete answer"),), "_"),
        )
    )
    renderer.render(RunEnd(run="run_success", status="succeeded"))

    assert capsys.readouterr().out == "assistant: complete answer\n"
    assert renderer.failure is None


@pytest.mark.parametrize("thread_id", (None, "term_existing"))
def test_interactive_tty_passes_the_unmodified_thread_to_the_tui(
    thread_id: str | None,
    monkeypatch: Any,
) -> None:
    client = _Client()
    captured: dict[str, object] = {}

    @contextmanager
    def runtime(*_args: object, **_kwargs: object) -> Iterator[_Client]:
        yield client

    def open_tui(
        _ctx: object,
        *,
        thread_id: str | None,
        selector_payload: dict[str, object] | None,
        client: object,
    ) -> None:
        captured.update(
            thread=thread_id,
            selectors=selector_payload,
            client=client,
        )

    monkeypatch.setattr(chat.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(chat.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(chat, "_chat_runtime", runtime)
    monkeypatch.setattr(chat, "_chat_interactive_prompt_toolkit", open_tui)

    chat._chat_interactive(
        object(),  # type: ignore[arg-type]
        thread_id=thread_id,
        selector_payload={"models": ["test/model"]},
    )

    assert captured == {
        "thread": thread_id,
        "selectors": {"models": ["test/model"]},
        "client": client,
    }
    assert client.created == 0


def test_chat_runtime_builds_process_local_execution_resources(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    captured: dict[str, object] = {}
    source = tmp_path / "alice.too"
    layout = AgentLayout.roaming(source)

    class Session(_Client):
        def __init__(self, layout: object, **kwargs: object) -> None:
            super().__init__()
            captured["layout"] = layout
            captured["kwargs"] = kwargs

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(chat, "context_layout", lambda _ctx: layout)
    monkeypatch.setattr(
        chat,
        "load_runtime_environ",
        lambda _layout, *, base_environ: {
            **base_environ,
            "TOOLANG_ALLOW_MODELS": "env/*",
            "TOOLANG_LIMIT_TIME": "30",
        },
    )
    monkeypatch.setattr(chat, "LocalChatSession", Session)

    with chat._chat_runtime(
        object(),  # type: ignore[arg-type]
        sandbox="host",
        allow_options=[
            "models=test/model",
            "tools=shell/*",
            "caps=skill/reviewer",
        ],
        default_options=["model=test/model", "runnable=agic:chat"],
        limit_options=["tokens=1000", "time=60"],
    ) as client:
        assert isinstance(client, Session)

    assert captured["layout"] == layout
    assert captured["kwargs"] == {
        "ceiling_overrides": {
            "models": ("test/model",),
            "tools": ("shell/*",),
            "caps": ("skill/reviewer",),
        },
        "binding_overrides": {
            "model": "test/model",
            "runnable": "agic:chat",
        },
        "limit_overrides": {"tokens": 1000, "time": 60},
    }
    assert captured["closed"] is True


def test_chat_runtime_uses_running_resident_without_local_environment(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    status = AgentStatus(
        name="alice",
        status="running",
        endpoint="http://127.0.0.1:7001",
        api_url=None,
        webui_url=None,
        sandbox="docker:python:3.13-slim",
    )
    captured: dict[str, object] = {}

    class Process:
        def __init__(self, selected: AgentLayout) -> None:
            assert selected == layout

        def status(self, *, ui_base_url: str) -> AgentStatus:
            assert ui_base_url == "https://ui.test"
            return status

    class Session(_Client):
        def __init__(self, endpoint: str, *, expected_sandbox: str) -> None:
            super().__init__()
            captured["endpoint"] = endpoint
            captured["sandbox"] = expected_sandbox

        def apply_settings(
            self,
            commands: tuple[RunOverride, ...],
            selects: Mapping[str, object],
        ) -> Mapping[str, object]:
            captured["commands"] = commands
            captured["selects"] = dict(selects)
            return {"remote": True}

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(chat, "context_layout", lambda _ctx: layout)
    monkeypatch.setattr(chat, "ui_base_url", lambda: "https://ui.test")
    monkeypatch.setattr(chat.agents, "AgentProcess", Process)
    monkeypatch.setattr(chat, "RemoteChatSession", Session)
    monkeypatch.setattr(
        chat,
        "load_runtime_environ",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("remote Chat must not load local runtime environment")
        ),
    )
    selectors: dict[str, object] = {"model": "existing/model"}

    with chat._chat_runtime(
        object(),  # type: ignore[arg-type]
        selector_payload=selectors,
        sandbox="docker",
        allow_options=["models=test/*"],
        default_options=["runnable=agic:chat"],
        limit_options=["cost=2.50"],
    ) as client:
        assert isinstance(client, Session)

    assert selectors == {"remote": True}
    assert captured == {
        "endpoint": "http://127.0.0.1:7001",
        "sandbox": "docker:python:3.13-slim",
        "commands": (
            RunOverride("allow", "models", ("test/*",)),
            RunOverride("default", "runnable", "agic:chat"),
            RunOverride("limit", "cost", Decimal("2.50")),
        ),
        "selects": {"model": "existing/model"},
        "closed": True,
    }


@pytest.mark.parametrize("runtime_status", ("preparing", "starting"))
def test_chat_runtime_fails_closed_for_unready_resident(
    runtime_status: str,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    status = AgentStatus(
        name="alice",
        status=runtime_status,
        endpoint="http://127.0.0.1:7001",
        api_url=None,
        webui_url=None,
        sandbox="host",
    )

    class Process:
        def __init__(self, _layout: AgentLayout) -> None:
            pass

        def status(self, *, ui_base_url: str) -> AgentStatus:
            del ui_base_url
            return status

    monkeypatch.setattr(chat, "context_layout", lambda _ctx: layout)
    monkeypatch.setattr(chat, "ui_base_url", lambda: "")
    monkeypatch.setattr(chat.agents, "AgentProcess", Process)

    with pytest.raises(click.ClickException, match=runtime_status):
        with chat._chat_runtime(
            object(),  # type: ignore[arg-type]
            sandbox=None,
        ):
            raise AssertionError("unready resident must not open Chat")


def test_chat_runtime_does_not_fall_back_after_remote_health_failure(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    status = AgentStatus(
        name="alice",
        status="running",
        endpoint="http://127.0.0.1:7001",
        api_url=None,
        webui_url=None,
        sandbox="host",
    )

    class Process:
        def __init__(self, _layout: AgentLayout) -> None:
            pass

        def status(self, *, ui_base_url: str) -> AgentStatus:
            del ui_base_url
            return status

    def failed_remote(*_args: object, **_kwargs: object) -> object:
        raise chat.RemoteChatError("remote chat health failed")

    monkeypatch.setattr(chat, "context_layout", lambda _ctx: layout)
    monkeypatch.setattr(chat, "ui_base_url", lambda: "")
    monkeypatch.setattr(chat.agents, "AgentProcess", Process)
    monkeypatch.setattr(chat, "RemoteChatSession", failed_remote)
    monkeypatch.setattr(
        chat,
        "LocalChatSession",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("running resident must not fall back to local execution")
        ),
    )

    with pytest.raises(click.ClickException, match="health failed"):
        with chat._chat_runtime(
            object(),  # type: ignore[arg-type]
            sandbox=None,
        ):
            raise AssertionError("failed remote must not open Chat")


def test_running_sandbox_match_accepts_driver_or_exact_spec() -> None:
    assert chat._sandbox_matches("docker", "docker:python:3.13-slim")
    assert chat._sandbox_matches(
        "docker:python:3.13-slim",
        "docker:python:3.13-slim",
    )
    assert not chat._sandbox_matches("host", "docker:python:3.13-slim")
    assert not chat._sandbox_matches("docker:other", "docker:python:3.13-slim")


def test_chat_ui_paths_follow_the_selected_layout(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    layout = AgentLayout.roaming(tmp_path / "alice.too")
    monkeypatch.setattr(chat, "context_layout", lambda _ctx: layout)

    history = chat._chat_input_history_store(object())  # type: ignore[arg-type]

    assert history is not None
    assert history.path == layout.runtime / "chat-input-history.jsonl"
    assert chat._chat_home_label(object()) == shorten_home_path(  # type: ignore[arg-type]
        layout.home
    )


def test_chat_runtime_rejects_hosted_sandboxes(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    layout = AgentLayout.roaming(tmp_path / "alice.too")
    monkeypatch.setattr(chat, "context_layout", lambda _ctx: layout)
    with pytest.raises(
        click.ClickException,
        match="supports only the host sandbox",
    ):
        with chat._chat_runtime(
            object(),  # type: ignore[arg-type]
            sandbox="docker",
        ):
            raise AssertionError("unsupported sandbox must not open a session")
