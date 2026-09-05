"""Terminal chat command orchestration."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import click
import pytest

from toolang.base.types.message import TextPart
from toolang.base.types.model import (
    ModelParameters,
    ModelRequest,
    ReasoningParameters,
)
from toolang.base.types.policy import RunPolicy
from toolang.cli.common.output import shorten_home_path
from toolang.cli.common.terminal_surfaces import TerminalSurfaces
from toolang.cli.toolang.commands.chat import main as chat
from toolang.cli.toolang.commands.chat.base import (
    ChatExecutorMetadata,
    ChatResult,
    ChatRunState,
)
from toolang.common.layout import AgentLayout
from toolang.execution.events import RunEnd, RunEvent, StepEnd
from toolang.execution.policy import apply_session_setting
from toolang.execution.schemas import RunRequest, RunnableRequest
from toolang.execution.types import (
    ErrorMessage,
    Local,
    ModelOverride,
    RunOverride,
    SessionSetting,
    StepRef,
)
from toolang.lang.input import RunnableInputRaw
from toolang.up.types import AgentServerRef

_HOST_DESCRIPTION = "macOS 27.0 arm64"


def test_chat_default_model_none_clears_the_configured_preference() -> None:
    update, clear_runnable = chat._chat_session_override(
        allow_options=None,
        default_options=["model=none"],
        limit_options=None,
    )

    assert update.model == ModelOverride(identity="unset")
    assert not clear_runnable


class _Client:
    executor_metadata = ChatExecutorMetadata(
        sandbox_selector="host",
        sandbox_detail=_HOST_DESCRIPTION,
    )

    def __init__(self) -> None:
        self.created = 0
        self.starts: list[tuple[str, str, ModelRequest | None]] = []

    def list_models(
        self,
        queries: Sequence[str] | None = None,
    ) -> Mapping[str, Any]:
        del queries
        return {"default": None, "items": []}

    def list_tools(
        self,
        queries: Sequence[str] | None = None,
    ) -> Mapping[str, Any]:
        del queries
        return {"items": []}

    def list_caps(
        self,
        kind: str | None = None,
        queries: Sequence[str] | None = None,
    ) -> Mapping[str, Any]:
        del kind, queries
        return {"items": []}

    def list_runnables(self, kind: str) -> Mapping[str, Any]:
        del kind
        return {"default": None, "items": []}

    def create_thread(self) -> str:
        self.created += 1
        return "term_created"

    def initial_setting(self) -> SessionSetting:
        return SessionSetting(
            model=ModelRequest("test/model"),
            runnable="agic:chat",
        )

    def apply_setting(
        self,
        setting: SessionSetting,
        update: RunOverride,
        *,
        allowed_model_refs: Collection[str] | None = None,
        default_model_ref: str | None = None,
    ) -> SessionSetting:
        del allowed_model_refs, default_model_ref
        return apply_session_setting(self.initial_setting(), setting, update)

    def build_request(
        self,
        thread_id: str,
        override: RunOverride,
        input: RunnableInputRaw,
        setting: SessionSetting,
    ) -> RunRequest:
        del override
        return RunRequest(
            thread_id=thread_id,
            request_id=f"request_{len(self.starts)}",
            runnable=RunnableRequest(
                setting.runnable or "agic:chat",
                input,
            ),
            model=setting.model,
            policy=RunPolicy(),
        )

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

    def run(
        self,
        request: RunRequest,
        on_event: Callable[[RunEvent], None],
        on_error: Callable[[str], None],
        on_state: Callable[[ChatRunState], None] | None = None,
    ) -> None:
        del on_event, on_error, on_state
        self.starts.append(
            (request.thread_id, request.runnable.input._ or "", request.model)
        )

    def cancel(self, run_id: str, on_error: Callable[[str], None]) -> None:
        del run_id, on_error

    def steer(
        self,
        run_id: str,
        message: str,
        on_error: Callable[[str], None],
    ) -> None:
        del run_id, message, on_error


class _FailedRunClient(_Client):
    def run(
        self,
        request: RunRequest,
        on_event: Callable[[RunEvent], None],
        on_error: Callable[[str], None],
        on_state: Callable[[ChatRunState], None] | None = None,
    ) -> None:
        del on_error, on_state
        self.starts.append(
            (request.thread_id, request.runnable.input._ or "", request.model)
        )
        on_event(
            RunEnd(
                run="run_failed",
                status="failed",
                error=ErrorMessage("provider failed"),
            )
        )


def test_scripted_chat_exit_does_not_create_an_empty_thread(
    monkeypatch: Any,
) -> None:
    client = _Client()
    inputs = iter(("/exit",))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    chat._chat_interactive_scripted_local(
        client=client,
        thread_id=None,
        setting=client.initial_setting(),
    )

    assert client.created == 0
    assert client.starts == []


def test_scripted_chat_help_does_not_create_an_empty_thread(
    monkeypatch: Any,
) -> None:
    client = _Client()
    inputs = iter(("/help", "/exit"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    chat._chat_interactive_scripted_local(
        client=client,
        thread_id=None,
        setting=client.initial_setting(),
    )

    assert client.created == 0
    assert client.starts == []


def test_scripted_chat_projects_shared_slash_outcomes(
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _Client()
    inputs = iter(("/model effort=high", "/model", "/models", "/exit"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    chat._chat_interactive_scripted_local(
        client=client,
        thread_id=None,
        setting=client.initial_setting(),
    )

    output = capsys.readouterr().out
    assert "Model set to test/model · high" in output
    assert "/model [MODEL] [effort=VALUE]" in output
    assert "Set the session model or effort" in output
    assert "0 models allowed." in output
    assert "Success:" not in output
    assert "Result:" not in output
    assert client.created == 0


def test_scripted_chat_projects_unrecognized_diagnostics_and_both_help_surfaces(
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _Client()
    inputs = iter(
        ("/", "/missing", ":", ":missing value", ":?", "/keys", "/?", "/exit")
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    chat._chat_interactive_scripted_local(
        client=client,
        thread_id=None,
        setting=client.initial_setting(),
    )

    captured = capsys.readouterr()
    assert "Enter a command after / · See /? for help" in captured.err
    assert "Unknown command /missing · See /? for help" in captured.err
    assert "Enter a run override after : · See :? for help" in captured.err
    assert "Unknown run override :missing · See :? for help" in captured.err
    assert "Run overrides change settings for this run only." in captured.out
    assert "These shortcuts control interactive Chat." in captured.out
    assert "Session commands:" in captured.out
    assert "To list one-run colon directives, type :?." in captured.out
    assert "Available overrides:" in captured.out
    assert "Available shortcuts:" in captured.out
    assert "Inspection commands:" in captured.out
    assert "Chat Commands" not in captured.out
    assert "Run Overrides" not in captured.out
    assert "Chat shortcuts" not in captured.out
    assert client.created == 0


@pytest.mark.parametrize("command", ["queue", "q", "steer", "s"])
def test_scripted_chat_rejects_removed_queue_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    client = _Client()
    inputs = iter((f"/{command} value", "/exit"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    chat._chat_interactive_scripted_local(
        client=client, thread_id=None, setting=client.initial_setting()
    )

    assert f"Unknown command /{command} · See /? for help" in capsys.readouterr().err
    assert client.created == 0
    assert not client.starts


def test_scripted_slash_help_honors_the_configured_maximum_width(
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _Client()
    inputs = iter(("/help", "/exit"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    chat._chat_interactive_scripted_local(
        client=client,
        thread_id=None,
        setting=client.initial_setting(),
        progress_max_width=48,
    )

    output = capsys.readouterr().out
    assert "Session commands:" in output
    assert all(len(line) <= 48 for line in output.splitlines())


def test_scripted_chat_creates_one_thread_for_the_first_submission(
    monkeypatch: Any,
) -> None:
    client = _Client()
    inputs = iter(("hello", "again", "/exit"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    chat._chat_interactive_scripted_local(
        client=client,
        thread_id=None,
        setting=client.initial_setting(),
    )

    assert client.created == 1
    assert client.starts == [
        ("term_created", "hello", ModelRequest("test/model")),
        ("term_created", "again", ModelRequest("test/model")),
    ]


def test_scripted_chat_reports_a_failed_run(
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _FailedRunClient()
    inputs = iter(("hello", "/exit"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    chat._chat_interactive_scripted_local(
        client=client,
        thread_id="term_existing",
        setting=client.initial_setting(),
    )

    assert "provider failed" in capsys.readouterr().err


def test_scripted_renderer_uses_model_step_output_without_deltas(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = chat._ScriptedRunRenderer()

    renderer.render(
        StepEnd(
            step=StepRef.parse("run_success.1"),
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
        setting: SessionSetting,
        client: object,
    ) -> None:
        captured.update(
            thread=thread_id,
            setting=setting,
            client=client,
        )

    monkeypatch.setattr(chat.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(chat.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(chat, "_chat_runtime", runtime)
    monkeypatch.setattr(chat, "_chat_interactive_prompt_toolkit", open_tui)

    chat._chat_interactive(
        object(),  # type: ignore[arg-type]
        thread_id=thread_id,
    )

    assert captured == {
        "thread": thread_id,
        "setting": client.initial_setting(),
        "client": client,
    }
    assert client.created == 0


def test_chat_invocation_defaults_initialize_the_session(
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
        setting: SessionSetting,
        client: object,
    ) -> None:
        del thread_id, client
        captured["setting"] = setting

    monkeypatch.setattr(chat.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(chat.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(chat, "_chat_runtime", runtime)
    monkeypatch.setattr(chat, "_chat_interactive_prompt_toolkit", open_tui)

    chat._chat_interactive(
        object(),  # type: ignore[arg-type]
        thread_id=None,
        default_options=[
            "model=test/other effort=high",
            "runnable=flow:review",
        ],
    )

    assert captured["setting"] == SessionSetting(
        model=ModelRequest(
            "test/other",
            ModelParameters(reasoning=ReasoningParameters(effort="high")),
        ),
        runnable="flow:review",
    )


def test_prompt_toolkit_resolves_surfaces_before_starting_the_tui(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    client = _Client()
    setting = client.initial_setting()
    environ = {
        "TOOLANG_COLOR_SCHEME": "#102030,#203040,#304050",
        "TOOLANG_PROGRESS_MAX_WIDTH": "72",
    }
    surfaces = TerminalSurfaces("#102030", "#203040", "#304050")
    calls: list[str] = []
    captured: dict[str, object] = {}

    monkeypatch.setattr(chat, "context_layout", lambda _ctx: layout)
    monkeypatch.setattr(
        chat,
        "load_runtime_environ",
        lambda selected, *, base_environ: (
            environ
            if selected == layout and base_environ is chat.os.environ
            else (_ for _ in ()).throw(AssertionError("unexpected environment load"))
        ),
    )

    def resolve(*, environment: Mapping[str, str]) -> TerminalSurfaces:
        calls.append("resolve")
        assert environment is environ
        return surfaces

    def run(**kwargs: object) -> None:
        calls.append("run")
        captured.update(kwargs)

    monkeypatch.setattr(chat, "resolve_terminal_surfaces", resolve)
    monkeypatch.setattr(chat.ChatTuiApp, "run", run)

    chat._chat_interactive_prompt_toolkit(
        object(),  # type: ignore[arg-type]
        thread_id="term_existing",
        setting=setting,
        client=client,
    )

    assert calls == ["resolve", "run"]
    assert captured["surfaces"] is surfaces
    assert captured["progress_max_width"] == 72
    assert captured["thread_id"] == "term_existing"
    assert captured["setting"] is setting
    assert captured["client"] is client


def test_prompt_toolkit_reports_invalid_color_scheme_before_tui_start(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    monkeypatch.setattr(chat, "context_layout", lambda _ctx: layout)
    monkeypatch.setattr(
        chat,
        "load_runtime_environ",
        lambda _layout, *, base_environ: {
            **base_environ,
            "TOOLANG_COLOR_SCHEME": "#111111,#222222",
        },
    )
    monkeypatch.setattr(
        chat.ChatTuiApp,
        "run",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("TUI must not start with invalid colors")
        ),
    )

    with pytest.raises(
        click.ClickException,
        match="three #RRGGBB colors in input,queue,code order",
    ):
        chat._chat_interactive_prompt_toolkit(
            object(),  # type: ignore[arg-type]
            thread_id=None,
            setting=_Client().initial_setting(),
            client=_Client(),
        )


def test_chat_default_options_build_session_override_without_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    override, clear_runnable = chat._chat_session_override(
        allow_options=None,
        default_options=["model=test/model effort=high"],
        limit_options=None,
    )

    assert override.model is not None
    assert override.model.identity == "test/model"
    assert override.model.effort == "high"
    assert not clear_runnable
    assert capsys.readouterr().err == ""

    cleared, clear_runnable = chat._chat_session_override(
        allow_options=None,
        default_options=["runnable=none"],
        limit_options=None,
    )
    assert cleared.empty
    assert clear_runnable
    assert capsys.readouterr().err == ""


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
    monkeypatch.setattr(chat, "ui_base_url", lambda: "https://ui.test")

    @contextmanager
    def agent_server_context(
        selected: AgentLayout,
        **kwargs: object,
    ) -> Iterator[AgentServerRef | None]:
        assert selected == layout
        assert kwargs == {
            "sandbox": "host",
            "dev": None,
            "model_catalog": None,
            "ui_base_url": "https://ui.test",
        }
        yield None

    monkeypatch.setattr(chat, "acquire_agent_server", agent_server_context)
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
    ) as client:
        assert isinstance(client, Session)

    assert captured["layout"] == layout
    assert captured["kwargs"] == {
        "sandbox": "host",
        "ceiling_overrides": {"models": ("env/*",)},
        "default_overrides": {},
        "limit_overrides": {"time": 30},
    }
    assert captured["closed"] is True


def test_chat_runtime_uses_remote_execution_without_local_environment(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    captured: dict[str, object] = {}

    @contextmanager
    def agent_server_context(
        selected: AgentLayout,
        **kwargs: object,
    ) -> Iterator[AgentServerRef]:
        assert selected == layout
        assert kwargs == {
            "sandbox": "docker",
            "dev": None,
            "model_catalog": None,
            "ui_base_url": "https://ui.test",
        }
        yield AgentServerRef(
            sandbox="docker:python:3.13-slim",
            endpoint="http://127.0.0.1:7001",
        )

    class Session(_Client):
        def __init__(self, endpoint: str, *, expected_sandbox: str) -> None:
            super().__init__()
            captured["endpoint"] = endpoint
            captured["sandbox"] = expected_sandbox

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(chat, "context_layout", lambda _ctx: layout)
    monkeypatch.setattr(chat, "ui_base_url", lambda: "https://ui.test")
    monkeypatch.setattr(chat, "acquire_agent_server", agent_server_context)
    monkeypatch.setattr(chat, "RemoteChatSession", Session)
    monkeypatch.setattr(
        chat,
        "load_runtime_environ",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("remote Chat must not load local runtime environment")
        ),
    )
    with chat._chat_runtime(
        object(),  # type: ignore[arg-type]
        sandbox="docker",
    ) as client:
        assert isinstance(client, Session)

    assert captured == {
        "endpoint": "http://127.0.0.1:7001",
        "sandbox": "docker:python:3.13-slim",
        "closed": True,
    }


def test_chat_runtime_does_not_fall_back_after_remote_health_failure(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")

    @contextmanager
    def agent_server_context(
        _layout: AgentLayout,
        **_kwargs: object,
    ) -> Iterator[AgentServerRef]:
        yield AgentServerRef(
            sandbox="host",
            endpoint="http://127.0.0.1:7001",
        )

    def failed_remote(*_args: object, **_kwargs: object) -> object:
        raise chat.RemoteChatError("remote chat health failed")

    monkeypatch.setattr(chat, "context_layout", lambda _ctx: layout)
    monkeypatch.setattr(chat, "ui_base_url", lambda: "")
    monkeypatch.setattr(chat, "acquire_agent_server", agent_server_context)
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


def test_chat_runtime_uses_a_temporary_remote_runtime(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    layout = AgentLayout.roaming(tmp_path / "alice.too")
    development = tmp_path / "dist"
    opened = False

    @contextmanager
    def agent_server_context(
        _layout: AgentLayout,
        **_kwargs: object,
    ) -> Iterator[AgentServerRef]:
        nonlocal opened
        assert _kwargs["dev"] == development
        opened = True
        yield AgentServerRef(
            sandbox="docker:python:3.13-slim",
            endpoint="http://127.0.0.1:8123",
        )

    class Session(_Client):
        def __init__(self, endpoint: str, *, expected_sandbox: str) -> None:
            super().__init__()
            assert endpoint == "http://127.0.0.1:8123"
            assert expected_sandbox == "docker:python:3.13-slim"

        def close(self) -> None:
            pass

    monkeypatch.setattr(chat, "context_layout", lambda _ctx: layout)
    monkeypatch.setattr(chat, "ui_base_url", lambda: "")
    monkeypatch.setattr(chat, "acquire_agent_server", agent_server_context)
    monkeypatch.setattr(chat, "RemoteChatSession", Session)

    with chat._chat_runtime(
        object(),  # type: ignore[arg-type]
        sandbox="docker",
        dev=development,
    ) as client:
        assert isinstance(client, Session)

    assert opened is True


def test_chat_runtime_closes_temporary_runtime_after_remote_initialization_failure(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    layout = AgentLayout.roaming(tmp_path / "alice.too")
    cleaned = False

    @contextmanager
    def agent_server_context(
        _layout: AgentLayout,
        **_kwargs: object,
    ) -> Iterator[AgentServerRef]:
        nonlocal cleaned
        try:
            yield AgentServerRef(
                sandbox="docker:python:3.13-slim",
                endpoint="http://127.0.0.1:8123",
            )
        finally:
            cleaned = True

    def failed_remote(*_args: object, **_kwargs: object) -> object:
        raise chat.RemoteChatError("temporary remote initialization failed")

    monkeypatch.setattr(chat, "context_layout", lambda _ctx: layout)
    monkeypatch.setattr(chat, "ui_base_url", lambda: "")
    monkeypatch.setattr(chat, "acquire_agent_server", agent_server_context)
    monkeypatch.setattr(chat, "RemoteChatSession", failed_remote)

    with pytest.raises(click.ClickException, match="initialization failed"):
        with chat._chat_runtime(
            object(),  # type: ignore[arg-type]
            sandbox="docker",
        ):
            raise AssertionError("failed remote must not open Chat")

    assert cleaned is True
