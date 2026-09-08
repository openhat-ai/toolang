"""Terminal chat command orchestration."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any
import sqlite3

import pytest
from typer._click.exceptions import ClickException

from tests.support.execution_fixtures import project_run_start
from toolang.base.types.message import Message, TextPart
from toolang.base.types.model import (
    ModelParameters,
    ModelRequest,
    ReasoningParameters,
)
from toolang.base.types.policy import RunPolicy
from toolang.cli.common.output import shorten_home_path
from toolang.cli.common.context import context_layout
from toolang.cli.common.terminal_surfaces import TerminalSurfaces
from toolang.cli.toolang.commands.chat import main as chat
from toolang.cli.toolang.commands.chat.base import (
    ChatExecutorMetadata,
    ChatResult,
    ChatRunState,
)
from toolang.cli.toolang.main import main as too_main
from toolang.common.layout import AgentLayout
from toolang.execution.events import RunEnd, RunEvent, StepEnd
from toolang.execution.policy import apply_session_setting
from toolang.execution.store import RunStore
from toolang.execution.schemas import ControlInfo, RunRequest, RunnableRequest
from toolang.execution.types import (
    Output,
    ErrorMessage,
    Local,
    ModelOverride,
    RunOverride,
    SessionSetting,
    StepRef,
)
from toolang.lang.input import CallInput
from toolang.up.types import AgentServerRef
from toolang.up import process as agents

_HOST_DESCRIPTION = "macOS 27.0 arm64"


@pytest.fixture
def chat_layout(tmp_path: Path) -> AgentLayout:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    (layout.home / "agent.too").write_text("agic chat:\n  Reply directly.\n")
    return layout


def _chat_history(layout: AgentLayout) -> None:
    with closing(RunStore(layout.run_store)) as store:
        store.create_thread(
            thread_id="term_existing", created_at="2026-01-01T00:00:00Z"
        )
        store.create_thread(thread_id="term_new", created_at="2026-01-02T00:00:00Z")
        project_run_start(
            store,
            run_id="run_existing",
            thread_id="term_existing",
            origin="chat",
            input=Message.user("hello"),
            created_at="2026-01-01T00:00:00Z",
        )
        store.finish_run(run_id="run_existing", finished_at="2026-01-03T00:00:00Z")
        assert store.list_threads()[0].id == "term_new"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], None),
        (["--thread"], "term_existing"),
        (["-t"], "term_existing"),
        (["--thread", "term_new"], "term_new"),
        (["-t", "term_new"], "term_new"),
        (["--thread=term_new"], "term_new"),
        (["-tterm_new"], "term_new"),
        (["--thread", "run_existing"], "term_existing"),
        (["-t", "run_existing"], "term_existing"),
        (["-t", "--sandbox", "host"], "term_existing"),
        (["--thread", "--default", "model=test/model"], "term_existing"),
        (["--thread", "--"], "term_existing"),
        (["-t", "term_new", "--thread"], "term_existing"),
        (["--thread", "-t", "term_new"], "term_new"),
    ],
)
def test_chat_thread_option_resolves_through_the_lazy_entry_point(
    args, expected, chat_layout, monkeypatch
):
    _chat_history(chat_layout)
    captured = {}

    def interactive(ctx, **kwargs):
        captured.update(layout=context_layout(ctx), **kwargs)

    monkeypatch.setattr(chat, "_chat_interactive", interactive)
    assert too_main(["--root", str(chat_layout.root), "alice", "chat", *args]) == 0
    assert captured["thread_id"] == expected
    assert captured["layout"] == chat_layout
    if "--sandbox" in args:
        assert captured["sandbox"] == "host"
    if "--default" in args:
        assert captured["default_options"] == ["model=test/model"]
    with closing(RunStore(chat_layout.run_store, read_only=True)) as store:
        assert len(store.list_threads()) == 2


@pytest.mark.parametrize("option", ["--thread", "-t"])
@pytest.mark.parametrize("history", ["missing", "empty", "incompatible"])
def test_chat_latest_requires_history_without_starting_a_runtime(
    option, history, chat_layout, monkeypatch, capsys
):
    if history != "missing":
        RunStore(chat_layout.run_store).close()
    if history == "incompatible":
        with sqlite3.connect(chat_layout.run_store) as connection:
            connection.execute("PRAGMA user_version = 99999")
    before = chat_layout.run_store.read_bytes() if history != "missing" else None
    monkeypatch.setattr(
        chat, "_chat_interactive", lambda *_args, **_kwargs: pytest.fail("started Chat")
    )
    assert too_main(["--root", str(chat_layout.root), "alice", "chat", option]) == 1
    error = capsys.readouterr().err
    if history == "incompatible":
        assert "execution history is incompatible" in error
    else:
        assert "no thread to resume" in error
        assert "omit --thread" in error
    after = (
        chat_layout.run_store.read_bytes() if chat_layout.run_store.exists() else None
    )
    assert after == before


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [
        (["term_existing"], 2, "unexpected extra argument"),
        (["--thread="], 2, "thread id must not be empty"),
        (["-t", ""], 2, "thread id must not be empty"),
        (["--thread", "--unknown"], 2, "No such option"),
        (["-t", "--", "term_existing"], 2, "unexpected extra argument"),
        (["--thread", "run_missing"], 1, "run not found: run_missing"),
        (["--dev"], 2, "requires an argument"),
    ],
)
def test_chat_thread_option_errors_preserve_existing_options(
    args, code, message, chat_layout, monkeypatch, capsys
):
    _chat_history(chat_layout)
    monkeypatch.setattr(
        chat, "_chat_interactive", lambda *_args, **_kwargs: pytest.fail("started Chat")
    )
    assert too_main(["--root", str(chat_layout.root), "alice", "chat", *args]) == code
    assert message in capsys.readouterr().err


def test_chat_latest_preserves_history_ties_and_includes_nonterminal_threads(
    chat_layout, monkeypatch
):
    with closing(RunStore(chat_layout.run_store)) as store:
        for thread in ("term_old", "web_new"):
            store.create_thread(thread_id=thread, created_at="2026-01-01T00:00:00Z")
        # Equal projected update times retain the history API's existing order.
        expected = chat.RunHistory(store).list_threads(limit=1)[0].id
    selected = []
    monkeypatch.setattr(
        chat,
        "_chat_interactive",
        lambda _ctx, **kwargs: selected.append(kwargs["thread_id"]),
    )
    assert too_main(["--root", str(chat_layout.root), "alice", "chat", "--thread"]) == 0
    assert selected == [expected]
    with closing(RunStore(chat_layout.run_store)) as store:
        store.create_thread(thread_id="web_latest", created_at="2026-01-04T00:00:00Z")
    assert too_main(["--root", str(chat_layout.root), "alice", "chat", "--thread"]) == 0
    assert selected[-1] == "web_latest"


@pytest.mark.parametrize("placement", ["resident", "roaming", "visiting"])
@pytest.mark.parametrize("remote", [False, True])
def test_chat_latest_uses_selected_history_for_local_and_remote_sessions(
    placement, remote, chat_layout, tmp_path, monkeypatch
):
    layout = chat_layout
    selector = "alice"
    if placement == "roaming":
        source = tmp_path / "alice.too"
        source.write_text("agic chat:\n  Reply directly.\n")
        layout = AgentLayout.roaming(source)
        selector = str(source)
    elif placement == "visiting":
        layout = AgentLayout(
            root=tmp_path / "visiting", name="alice", placement="visiting"
        )
        selector = "owner/alice"
        monkeypatch.setattr(
            agents, "resolve_visiting_layout", lambda *_args, **_kwargs: layout
        )
    if placement != "resident":
        with closing(RunStore(chat_layout.run_store)) as store:
            store.create_thread(
                thread_id="term_unrelated", created_at="2099-01-01T00:00:00Z"
            )
    _chat_history(layout)
    captured = {}

    class Session(_Client):
        def __init__(self, *args, **_kwargs):
            super().__init__()
            captured["target"] = args[0]

        def close(self):
            captured["closed"] = True

    @contextmanager
    def acquire(selected, **_kwargs):
        assert selected == layout
        yield (
            AgentServerRef(sandbox="host", endpoint="http://localhost:7001")
            if remote
            else None
        )

    def open_tui(ctx, **kwargs):
        captured.update(layout=context_layout(ctx), thread=kwargs["thread_id"])
        assert kwargs["client"].created == 0

    monkeypatch.setattr(chat, "acquire_agent_server", acquire)
    monkeypatch.setattr(chat, "LocalChatSession", Session)
    monkeypatch.setattr(chat, "RemoteChatSession", Session)
    monkeypatch.setattr(chat, "load_runtime_environ", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(chat, "_chat_interactive_prompt_toolkit", open_tui)
    monkeypatch.setattr(chat.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(chat.sys.stdout, "isatty", lambda: True)
    prefix = [] if placement == "roaming" else ["--root", str(chat_layout.root)]
    monkeypatch.setenv("TOOLANG_ROOT", str(chat_layout.root))
    assert too_main([*prefix, selector, "chat", "--thread"]) == 0
    assert captured == {
        "target": "http://localhost:7001" if remote else layout,
        "layout": layout,
        "thread": "term_existing",
        "closed": True,
    }


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
        input: CallInput[str],
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
            (request.thread_id, request.runnable.input.get("_") or "", request.model)
        )

    def cancel(self, run_id: str, on_error: Callable[[str], None]) -> None:
        del run_id, on_error

    def steer(
        self,
        run_id: str,
        message: str,
        on_error: Callable[[str], None],
        on_control: Callable[[ControlInfo], None] | None = None,
    ) -> None:
        del run_id, message, on_error, on_control


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
            (request.thread_id, request.runnable.input.get("_") or "", request.model)
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
            output=Output(Local.typed("Part[]", (TextPart("complete answer"),)), "_"),
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
        ClickException,
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
            "compact_override": ModelOverride(identity="test/compact", effort="low"),
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
            "TOOLANG_COMPACT_MODEL": "test/environment effort=high",
        },
    )
    monkeypatch.setattr(chat, "LocalChatSession", Session)

    with chat._chat_runtime(
        object(),  # type: ignore[arg-type]
        sandbox="host",
        compact_model="test/compact effort=low",
    ) as client:
        assert isinstance(client, Session)

    assert captured["layout"] == layout
    assert captured["kwargs"] == {
        "sandbox": "host",
        "ceiling_overrides": {"models": ("env/*",)},
        "default_overrides": {},
        "limit_overrides": {"time": 30},
        "compact_override": ModelOverride(identity="test/compact", effort="low"),
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
            "compact_override": None,
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

    with pytest.raises(ClickException, match="health failed"):
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

    with pytest.raises(ClickException, match="initialization failed"):
        with chat._chat_runtime(
            object(),  # type: ignore[arg-type]
            sandbox="docker",
        ):
            raise AssertionError("failed remote must not open Chat")

    assert cleaned is True
