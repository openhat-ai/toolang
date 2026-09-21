"""Tmux placement: session naming, the launcher operations, and chat dispatch."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from toolang.cli.common import tmux
from toolang.cli.common.errors import TmuxPlacementError
from toolang.common.layout import AgentLayout
from toolang.execution.inspection.history import RunHistory
from toolang.execution.store import RunStore
from typer._click.exceptions import ClickException
from toolang.cli.toolang.commands.chat import main as chat

TMUX_ENV = {"TMUX": "/tmp/tmux-1/sock,1,0", "TMUX_PANE": "%3"}


class FakePad:
    """One pane of a fake window, with the identity the protocol needs."""

    def __init__(self, window: FakeWindow) -> None:
        self.pane_id = "%1"
        self.session_id = "$0"
        self.window = window
        self.options: dict[str, str] = {}
        self.selected = False
        self.pane_dead = "0"
        self.respawned: list[str | None] = []

    def respawn(
        self, *, shell: str | None = None, start_directory: str | None = None
    ) -> object:
        self.respawned.append(shell)
        self.pane_dead = "0"
        return self

    def show_option(self, option: str) -> Any:
        return self.options.get(option)

    def set_option(self, option: str, value: str) -> object:
        self.options[option] = value
        return self

    def unset_option(self, option: str) -> object:
        self.options.pop(option, None)
        return self

    def select(self) -> object:
        self.selected = True
        return self


class FakeWindow:
    """A window that records options, splits, selection, and its session."""

    def __init__(self, window_id: str, name: str = "zsh") -> None:
        self.window_id = window_id
        self.window_name = name
        self.session: FakeSession | None = None
        self.options: dict[str, str] = {}
        self.selected = False
        self.killed = False
        self.panes: list[FakePad] = [FakePad(self)]
        self.pads: list[tuple[str | None, str | None]] = []

    def show_option(self, option: str) -> Any:
        return self.options.get(option)

    def set_option(self, option: str, value: str) -> object:
        self.options[option] = value
        return self

    def select(self) -> object:
        self.selected = True
        return self

    def kill_window(self) -> object:
        self.killed = True
        return self

    def unset_option(self, option: str) -> object:
        self.options.pop(option, None)
        return self

    def rename_window(self, new_name: str) -> object:
        self.window_name = new_name
        return self

    def split(
        self,
        *,
        start_directory: str | None = None,
        shell: str | None = None,
        attach: bool = False,
    ) -> FakePad:
        assert attach is False
        self.pads.append((start_directory, shell))
        pane = FakePad(self)
        pane.pane_id = f"%{len(self.panes) + 1}"
        self.panes.append(pane)
        return pane


class FakeSession:
    """A session that records session options and opened windows."""

    def __init__(self, session_id: str, name: str) -> None:
        self.session_id = session_id
        self.session_name = name
        self.options: dict[str, str] = {}
        self.opened: list[tuple[str | None, str | None, str | None]] = []
        self._windows: list[FakeWindow] = []

    def add_window(self, window: FakeWindow) -> FakeWindow:
        window.session = self
        self._windows.append(window)
        return window

    @property
    def windows(self) -> list[FakeWindow]:
        return list(self._windows)

    @property
    def active_window(self) -> FakeWindow:
        return self._windows[0]

    def show_option(self, option: str) -> Any:
        return self.options.get(option)

    def set_option(self, option: str, value: str) -> object:
        self.options[option] = value
        return self

    def select_window(self, target_window: str) -> object:
        window = next(w for w in self._windows if w.window_id == target_window)
        window.selected = True
        return window

    def new_window(
        self,
        window_name: str | None = None,
        *,
        start_directory: str | None = None,
        window_shell: str | None = None,
        attach: bool = False,
    ) -> FakeWindow:
        assert attach is False
        self.opened.append((window_name, start_directory, window_shell))
        window = FakeWindow(f"@w{len(self._windows)}", window_name or "sh")
        return self.add_window(window)


class FakeServer:
    """A server that records created sessions, switches, and attaches."""

    def __init__(self, sessions: list[FakeSession] | None = None) -> None:
        self._sessions = list(sessions or [])
        self.created: list[tuple[str, str | None, str | None]] = []
        self.switched: list[str] = []
        self.attached: list[str] = []
        self.clients: list[object] = []

    @property
    def sessions(self) -> list[FakeSession]:
        return list(self._sessions)

    def new_session(
        self,
        session_name: str,
        *,
        attach: bool = False,
        start_directory: str | None = None,
        window_command: str | None = None,
    ) -> FakeSession:
        assert attach is False
        self.created.append((session_name, start_directory, window_command))
        number = max((int(s.session_id[1:]) for s in self._sessions), default=-1) + 1
        session = FakeSession(f"${number}", session_name)
        if window_command is not None:
            session.add_window(FakeWindow("@new", "sh"))
        self._sessions.append(session)
        return session

    def switch_client(self, target_session: str) -> object:
        self.switched.append(target_session)
        return self

    def attach_session(self, target_session: str) -> object:
        self.attached.append(target_session)
        return self


class BrokenServer(FakeServer):
    """A server that refuses to create sessions."""

    def new_session(
        self,
        session_name: str,
        *,
        attach: bool = False,
        start_directory: str | None = None,
        window_command: str | None = None,
    ) -> FakeSession:
        del attach, start_directory, window_command
        raise RuntimeError(f"{session_name} refused")


class FakePane:
    """The pane chat runs in, reduced to its identity."""

    def __init__(self, session_id: str = "$0", pane_id: str = "%3") -> None:
        self.pane_id = pane_id
        self.session_id = session_id


class _Layout:
    name = "eve"


def _launcher(server: FakeServer, pane: FakePane, agent: str = "eve") -> tmux.Launcher:
    launcher = tmux.resolve_launcher(
        agent=agent,
        environment=TMUX_ENV,
        server_factory=lambda: server,
        pane_factory=lambda: pane,
    )
    assert launcher is not None
    return launcher


def _place(
    monkeypatch: pytest.MonkeyPatch,
    launcher: tmux.Launcher | None,
    *,
    thread_id: str | None = None,
    argv: list[str] | None = None,
) -> bool:
    """Run the chat placement decision with one fixed launcher."""

    if launcher is None:
        return True
    try:
        return launcher.place_chat(
            thread_id=thread_id or "term_new",
            directory="/work",
            argv=["too", "eve", "chat"] if argv is None else argv,
        )
    except TmuxPlacementError as exc:
        raise ClickException(str(exc)) from exc


@pytest.mark.parametrize(
    ("agent", "expected"),
    (
        ("eve", "eve"),
        ("TA", "ta"),
        ("my agent", "my-agent"),
        ("a.b:c", "a-b-c"),
        ("path/to.eve", "path-to-eve"),
        ("--", "agent"),
        ("", "agent"),
    ),
)
def test_sanitize_session_name_derives_a_tmux_name(agent: str, expected: str) -> None:
    assert tmux.sanitize_session_name(agent) == expected


def test_resolve_launcher_outside_tmux_reads_nothing() -> None:
    def boom() -> Any:
        raise AssertionError("must not touch tmux")

    assert (
        tmux.resolve_launcher(
            agent="eve", environment={}, server_factory=boom, pane_factory=boom
        )
        is None
    )


def test_resolve_launcher_honours_the_disable_switch() -> None:
    def boom() -> Any:
        raise AssertionError("must not touch tmux")

    assert (
        tmux.resolve_launcher(
            agent="eve",
            environment={**TMUX_ENV, tmux.ENABLED_ENV: "0"},
            server_factory=boom,
            pane_factory=boom,
        )
        is None
    )


def test_resolve_launcher_ignores_a_failing_lookup() -> None:
    def boom() -> Any:
        raise RuntimeError("no server running")

    assert (
        tmux.resolve_launcher(
            agent="eve",
            environment=TMUX_ENV,
            server_factory=boom,
            pane_factory=boom,
        )
        is None
    )


def test_agent_session_prefers_the_session_option() -> None:
    named = FakeSession("$0", "eve")
    marked = FakeSession("$1", "unrelated")
    marked.set_option(tmux.SESSION_AGENT, "eve")
    launcher = _launcher(FakeServer([named, marked]), FakePane(session_id="$9"))

    assert launcher.agent_session() is marked


def test_agent_session_falls_back_to_an_unowned_name() -> None:
    named = FakeSession("$0", "eve")
    launcher = _launcher(FakeServer([named]), FakePane(session_id="$9"))

    assert launcher.agent_session() is named


def test_agent_session_marks_a_session_it_adopts_by_name() -> None:
    """Determining the session records the agent mark, so the name is a fallback."""

    named = FakeSession("$0", "eve")
    launcher = _launcher(FakeServer([named]), FakePane(session_id="$9"))

    assert launcher.agent_session() is named
    assert named.show_option(tmux.SESSION_AGENT) == "eve"


def test_agent_session_skips_a_name_another_agent_owns() -> None:
    foreign = FakeSession("$0", "eve")
    foreign.set_option(tmux.SESSION_AGENT, "ta")
    launcher = _launcher(FakeServer([foreign]), FakePane(session_id="$9"))

    assert launcher.agent_session() is None


def test_is_current_compares_the_pane_session() -> None:
    session = FakeSession("$0", "eve")
    launcher = _launcher(FakeServer([session]), FakePane(session_id="$1"))

    assert launcher.is_current(session) is False


def test_chat_pad_finds_a_pad_in_any_pane() -> None:
    window = FakeWindow("@1")
    launcher = _launcher(
        FakeServer([FakeSession("$0", "eve")]), FakePane(session_id="$9")
    )

    assert launcher.chat_pad(window) is None

    background = FakePad(window)
    background.options[tmux.MARK_PAD] = tmux.PAD_CHAT
    window.panes.append(background)

    assert launcher.chat_pad(window) is background


def test_name_window_renames_a_container() -> None:
    window = FakeWindow("@1")
    launcher = _launcher(
        FakeServer([FakeSession("$0", "eve")]), FakePane(session_id="$9")
    )

    launcher.name_window(window, "term_x")

    assert window.window_name == "term_x"


def test_mark_thread_records_the_window_option() -> None:
    window = FakeWindow("@1")
    launcher = _launcher(
        FakeServer([FakeSession("$0", "eve")]), FakePane(session_id="$9")
    )

    launcher.mark_thread(window, "term_x")

    assert window.options[tmux.MARK_THREAD] == "term_x"


def test_open_pad_splits_the_window_with_the_command() -> None:
    window = FakeWindow("@1")
    launcher = _launcher(
        FakeServer([FakeSession("$0", "eve")]), FakePane(session_id="$9")
    )

    pad = launcher.open_pad(window, command="too eve chat", directory="/work")
    assert pad is window.panes[-1]
    assert window.pads == [("/work", "too eve chat")]


def test_thread_window_picks_the_newest_match() -> None:
    session = FakeSession("$0", "eve")
    older = session.add_window(FakeWindow("@1"))
    older.set_option(tmux.MARK_THREAD, "term_x")
    other = session.add_window(FakeWindow("@2"))
    other.set_option(tmux.MARK_THREAD, "term_y")
    newer = session.add_window(FakeWindow("@3"))
    newer.set_option(tmux.MARK_THREAD, "term_x")
    launcher = _launcher(FakeServer([session]), FakePane(session_id="$0"))

    assert launcher.thread_window(session, "term_x") is newer
    assert launcher.thread_window(session, "term_missing") is None


def test_thread_window_ignores_the_old_metadata_key() -> None:
    session = FakeSession("$0", "eve")
    legacy = session.add_window(FakeWindow("@1", "term_x"))
    legacy.set_option("@toolang_thread_id", "term_x")
    renamed = session.add_window(FakeWindow("@2", "user-renamed"))
    renamed.set_option("@toolang_thread", "term_y")
    launcher = _launcher(FakeServer([session]), FakePane())

    assert launcher.thread_window(session, "term_x") is None
    assert launcher.thread_window(session, "term_y") is renamed


@pytest.mark.parametrize(
    "input_tty, output_tty", [(False, False), (True, False), (False, True)]
)
def test_non_tty_chat_does_not_resolve_a_launcher(
    monkeypatch: pytest.MonkeyPatch, input_tty: bool, output_tty: bool
) -> None:
    monkeypatch.setattr(chat.sys.stdin, "isatty", lambda: input_tty)
    monkeypatch.setattr(chat.sys.stdout, "isatty", lambda: output_tty)
    monkeypatch.setenv("TMUX", TMUX_ENV["TMUX"])
    monkeypatch.setenv("TMUX_PANE", TMUX_ENV["TMUX_PANE"])

    def forbidden(**_kwargs: object) -> None:
        raise AssertionError("non-TTY chat must not resolve or mutate tmux")

    monkeypatch.setattr(chat, "resolve_launcher", forbidden)
    monkeypatch.setattr(chat, "_chat_interactive", lambda *_args, **_kwargs: None)
    chat.chat_command(cast(Any, None))


def test_ensure_session_creates_the_agent_session_with_its_chat_window() -> None:
    shell = FakeSession("$0", "shell")
    server = FakeServer([shell])
    launcher = _launcher(server, FakePane(session_id="$0"))

    session, window = launcher.ensure_session(command="too eve chat", directory="/work")

    assert session is not None
    assert window is not None
    assert server.created == [("eve", "/work", "too eve chat")]
    assert session.show_option(tmux.SESSION_AGENT) == "eve"
    assert window.session is session


def test_ensure_session_suffixes_a_name_another_agent_owns() -> None:
    foreign = FakeSession("$0", "eve")
    foreign.set_option(tmux.SESSION_AGENT, "ta")
    server = FakeServer([foreign])
    launcher = _launcher(server, FakePane(session_id="$0"))

    session, _window = launcher.ensure_session(command="c", directory="/work")

    assert server.created == [("eve-2", "/work", "c")]
    assert session is not None
    assert session.show_option(tmux.SESSION_AGENT) == "eve"


def test_ensure_session_adopts_a_name_matched_session() -> None:
    named = FakeSession("$0", "eve")
    named.add_window(FakeWindow("@0"))
    server = FakeServer([named])
    launcher = _launcher(server, FakePane(session_id="$9"))

    session, window = launcher.ensure_session(command="c")

    assert session is named
    assert window is None
    assert named.options[tmux.SESSION_AGENT] == "eve"
    assert server.created == []


def test_ensure_session_keeps_the_client_when_the_new_session_dies() -> None:
    server = FakeServer([FakeSession("$0", "shell")])
    launcher = _launcher(server, FakePane(session_id="$0"))

    session, _window = launcher.ensure_session(command="c")

    assert session is not None
    assert session.show_option(tmux.DETACH_ON_DESTROY) == "off"


def test_ensure_session_leaves_an_existing_session_alone() -> None:
    named = FakeSession("$0", "eve")
    named.add_window(FakeWindow("@0"))
    launcher = _launcher(FakeServer([named]), FakePane(session_id="$9"))

    launcher.ensure_session(command="c")

    assert tmux.DETACH_ON_DESTROY not in named.options


def test_ensure_session_reports_a_refused_creation() -> None:
    launcher = _launcher(BrokenServer(), FakePane(session_id="$9"))

    with pytest.raises(
        TmuxPlacementError, match="Could not create tmux session.*refused"
    ):
        launcher.ensure_session(command="c")


def test_open_window_reports_a_refused_window() -> None:
    class Refusing(FakeSession):
        def new_window(self, *args: Any, **kwargs: Any) -> FakeWindow:
            raise RuntimeError("no space")

    session = Refusing("$0", "eve")
    launcher = _launcher(FakeServer([session]), FakePane(session_id="$9"))

    with pytest.raises(
        TmuxPlacementError, match="Could not create tmux chat window: no space"
    ):
        launcher.open_window(session, command="c")


@pytest.mark.parametrize("same_session", [True, False])
def test_select_target_switches_only_across_sessions(same_session: bool) -> None:
    session = FakeSession("$0", "user-renamed")
    window = session.add_window(FakeWindow("@1", "renamed-thread"))
    pad = window.panes[0]
    server = FakeServer([session])
    launcher = _launcher(server, FakePane(session_id="$0" if same_session else "$9"))

    assert launcher.select_target(window, pane=pad) is True
    assert window.selected is True
    assert pad.selected is True
    assert server.switched == ([] if same_session else ["$0"])
    assert server.attached == []


def test_list_windows_ignores_a_session_that_will_not_list() -> None:
    class Unlistable(FakeSession):
        @property
        def windows(self) -> list[FakeWindow]:
            raise RuntimeError("session gone")

    session = Unlistable("$0", "eve")
    launcher = _launcher(FakeServer([session]), FakePane(session_id="$0"))

    assert launcher.list_windows(session) == ()
    assert launcher.thread_window(session, "term_x") is None


def test_place_chat_runs_in_place_outside_tmux(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _place(monkeypatch, None) is True
    assert capsys.readouterr().out == ""


def test_new_chat_creates_a_window_even_in_the_agent_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession("$0", "eve")
    server = FakeServer([session])
    launcher = _launcher(server, FakePane(session_id="$0"))

    assert _place(monkeypatch, launcher) is False
    assert server.switched == []
    assert server.created == []
    assert len(session.opened) == 1
    assert "created chat pane" in capsys.readouterr().out


@pytest.mark.parametrize("same_session", [True, False])
@pytest.mark.parametrize("live_pad", [True, False])
def test_place_chat_reuses_or_creates_pad_and_enters_target(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    same_session: bool,
    live_pad: bool,
) -> None:
    session = FakeSession("$0", "renamed-agent")
    session.set_option(tmux.SESSION_AGENT, "eve")
    window = session.add_window(FakeWindow("@1", "renamed-thread"))
    window.set_option(tmux.MARK_THREAD, "term_x")
    if live_pad:
        window.panes[0].options[tmux.MARK_PAD] = tmux.PAD_CHAT
    server = FakeServer([session])
    launcher = _launcher(server, FakePane(session_id="$0" if same_session else "$9"))

    assert _place(monkeypatch, launcher, thread_id="term_x") is False

    assert server.created == []
    assert session.opened == []
    assert len(window.pads) == (0 if live_pad else 1)
    assert window.selected
    assert window.panes[-1].selected
    assert server.switched == ([] if same_session else ["$0"])
    assert not server.attached
    out = capsys.readouterr().out
    assert ("reused chat pane" if live_pad else "created chat pane") in out
    assert "renamed-agent ($0), window renamed-thread (@1)" in out
    assert "; selected" in out


@pytest.mark.parametrize("session_exists", [True, False])
@pytest.mark.parametrize("thread_id", [None, "term_x"])
def test_place_chat_creates_missing_target_detached(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    session_exists: bool,
    thread_id: str | None,
) -> None:
    shell = FakeSession("$0", "shell")
    agent = FakeSession("$1", "eve")
    agent.add_window(FakeWindow("@0"))
    server = FakeServer([shell, agent] if session_exists else [shell])
    launcher = _launcher(server, FakePane(session_id="$0"))

    assert _place(monkeypatch, launcher, thread_id=thread_id) is False

    target = server.sessions[-1]
    window = target.windows[-1]
    assert len(server.created) == (0 if session_exists else 1)
    assert len(target.opened) == (1 if session_exists else 0)
    assert window.window_name == (thread_id or "term_new")
    assert window.options.get(tmux.MARK_THREAD) == (thread_id or "term_new")
    assert window.selected and not window.killed
    assert server.switched == [target.session_id]
    assert not server.attached
    assert window.panes[0].options[tmux.MARK_PAD] == tmux.PAD_CHAT
    assert "created chat pane" in capsys.readouterr().out


def test_place_chat_reports_creation_error_here(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    launcher = _launcher(BrokenServer(), FakePane(session_id="$9"))

    with pytest.raises(
        ClickException, match="Could not create tmux session eve: eve refused"
    ) as error:
        _place(monkeypatch, launcher)
    error.value.show()
    assert error.value.exit_code == 1
    assert "eve refused" in capsys.readouterr().err


@pytest.mark.parametrize("live_pad", [True, False])
def test_failed_selection_keeps_prepared_target_and_reports_location(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    live_pad: bool,
) -> None:
    class Unselectable(FakeSession):
        def select_window(self, target_window: str) -> object:
            raise RuntimeError("selection refused")

    session = Unselectable("$0", "eve")
    window = session.add_window(FakeWindow("@1", "term_x"))
    window.set_option(tmux.MARK_THREAD, "term_x")
    if live_pad:
        window.panes[0].options[tmux.MARK_PAD] = tmux.PAD_CHAT
    server = FakeServer([session])
    launcher = _launcher(server, FakePane(session_id="$0"))

    with pytest.raises(
        ClickException, match="selection refused.*target was kept"
    ) as error:
        _place(monkeypatch, launcher, thread_id="term_x")
    error.value.show()

    assert len(window.pads) == (0 if live_pad else 1)
    assert window.killed is False
    assert not server.created and not server.switched and not server.attached
    captured = capsys.readouterr()
    assert "window term_x (@1), pane" in captured.err
    assert "selection refused" in captured.err


def test_place_chat_runs_in_its_current_marked_pad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession("$0", "eve")
    window = session.add_window(FakeWindow("@1"))
    window.set_option(tmux.MARK_THREAD, "term_x")
    window.panes[0].set_option(tmux.MARK_PAD, tmux.PAD_CHAT)
    launcher = _launcher(FakeServer([session]), FakePane(pane_id="%1"))

    assert _place(monkeypatch, launcher, thread_id="term_x") is True
    assert not window.pads


@pytest.mark.parametrize("missing", ["window", "pane"])
def test_place_chat_displays_window_or_pane_creation_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str,
) -> None:
    session = FakeSession("$0", "eve")
    window = session.add_window(FakeWindow("@1"))
    if missing == "pane":
        window.set_option(tmux.MARK_THREAD, "term_x")
    server = FakeServer([session])
    launcher = _launcher(server, FakePane(session_id="$9"))

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("no space for a new pane")

    if missing == "pane":
        monkeypatch.setattr(window, "split", refuse)
    else:
        monkeypatch.setattr(session, "new_window", refuse)
    with pytest.raises(ClickException, match="no space for a new pane") as error:
        _place(monkeypatch, launcher, thread_id="term_x")
    error.value.show()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no space for a new pane" in captured.err
    assert not window.killed and not window.pads and not session.opened


def test_cli_allocates_thread_before_launch_and_preserves_parsed_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = AgentLayout.resident(tmp_path / "root with spaces", "chat")
    captured: dict[str, Any] = {}

    class RecordingLauncher:
        def place_chat(self, **kwargs: Any) -> bool:
            store = RunStore(layout.run_store, read_only=True)
            try:
                detail = RunHistory(store).get_thread(kwargs["thread_id"], run_limit=0)
                assert detail is not None and detail.run_count == 0
            finally:
                store.close()
            captured.update(kwargs)
            return False

    monkeypatch.setattr(chat.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(chat.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(chat, "context_layout", lambda _ctx: layout)
    monkeypatch.setattr(chat, "resolve_launcher", lambda **_kwargs: RecordingLauncher())
    monkeypatch.setattr(
        chat,
        "_chat_interactive",
        lambda *_args, **_kwargs: pytest.fail("must hand off"),
    )
    chat.chat_command(
        cast(Any, None),
        sandbox="docker:cfg",
        dev=Path("my wheel"),
        model_catalog=Path("catalog.toml"),
        allows=["tools=a,b"],
        defaults=["runnable=agic:chat"],
        limits=["steps=5"],
        compact_model="provider/model",
    )
    assert captured["thread_id"].startswith("term_")
    assert captured["argv"] == [
        chat.sys.executable,
        "-m",
        "toolang.cli.toolang",
        "--root",
        str(layout.root),
        "agent:chat",
        "chat",
        "--thread",
        captured["thread_id"],
        "--catalog=catalog.toml",
        "--sandbox=docker:cfg",
        "--dev=my wheel",
        "--compact-model=provider/model",
        "--allow=tools=a,b",
        "--default=runnable=agic:chat",
        "--limit=steps=5",
    ]


@pytest.mark.parametrize("environment", [{}, {**TMUX_ENV, "TOOLANG_TMUX": "0"}])
def test_direct_chat_does_not_allocate_thread_or_touch_tmux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment: dict[str, str]
) -> None:
    layout = AgentLayout.resident(tmp_path, "eve")
    for key in ("TMUX", "TMUX_PANE", "TOOLANG_TMUX"):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(chat.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(chat.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(chat, "context_layout", lambda _ctx: layout)
    monkeypatch.setattr(
        tmux, "_libtmux_server", lambda: pytest.fail("must not query tmux")
    )
    calls: list[str | None] = []
    monkeypatch.setattr(
        chat,
        "_chat_interactive",
        lambda _ctx, **kwargs: calls.append(kwargs["thread_id"]),
    )
    chat.chat_command(cast(Any, None))
    assert calls == [None]
    assert not layout.run_store.exists()
    assert tmux.resolve_marks() is None


def test_tmux_thread_reuses_validates_and_keeps_empty_threads(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "eve")
    first = chat._tmux_thread(layout, None)
    assert chat._tmux_thread(layout, first) == first
    second = chat._tmux_thread(layout, None)
    assert second != first
    with pytest.raises(ClickException, match="thread not found"):
        chat._tmux_thread(layout, "term_missing")
    store = RunStore(layout.run_store, read_only=True)
    try:
        threads = RunHistory(store).list_threads()
        assert {thread.id for thread in threads} == {first, second}
        assert all(thread.run_count == 0 for thread in threads)
    finally:
        store.close()


def test_current_unmarked_pane_is_used_only_in_the_exact_thread_window() -> None:
    session = FakeSession("$0", "eve")
    window = session.add_window(FakeWindow("@1", "custom"))
    window.options[tmux.MARK_THREAD] = "term_x"
    source = window.panes[0]
    launcher = _launcher(FakeServer([session]), cast(Any, source))
    assert launcher.place_chat(thread_id="term_x", argv=["too"], directory="/work")
    assert not window.pads and not session.opened and not window.selected
    assert window.window_name == "custom"


def test_failed_chat_is_restarted_without_replacing_a_live_process() -> None:
    session = FakeSession("$0", "eve")
    window = session.add_window(FakeWindow("@1"))
    window.options[tmux.MARK_THREAD] = "term_x"
    failed = window.panes[0]
    failed.options[tmux.MARK_PAD] = "chat"
    failed.pane_dead = "1"
    server = FakeServer([session])
    launcher = _launcher(server, FakePane(session_id="$9"))
    assert launcher.chat_pad(window) is None
    assert not launcher.place_chat(thread_id="term_x", argv=["too"], directory="/work")
    assert failed.respawned == [launcher.chat_command(["too"])]
    assert not window.pads
    assert server.switched == ["$0"]


def test_same_window_navigation_only_selects_the_pane() -> None:
    session = FakeSession("$0", "eve")
    window = session.add_window(FakeWindow("@1"))
    target = window.split()
    launcher = _launcher(FakeServer([session]), cast(Any, window.panes[0]))
    assert launcher.select_target(window, pane=target)
    assert target.selected and not window.selected


def test_in_place_publication_preserves_a_renamed_thread_window() -> None:
    window = FakeWindow("@1", "user title")
    window.options[tmux.MARK_THREAD] = "term_x"
    marks = tmux.resolve_marks(
        environment=TMUX_ENV, pane_factory=lambda: window.panes[0]
    )
    lifecycle = chat.ChatMarks(marks=marks)
    lifecycle.start("term_x")
    assert window.window_name == "user title"
    assert window.panes[0].options[tmux.MARK_PAD] == "chat"
    lifecycle.clear()
    assert tmux.MARK_PAD not in window.panes[0].options
    assert window.options[tmux.MARK_THREAD] == "term_x"


def test_launcher_reports_identity_publication_failure() -> None:
    class Unmarkable(FakeSession):
        def set_option(self, option: str, value: str) -> object:
            raise RuntimeError("session mark refused")

    session = Unmarkable("$0", "eve")
    launcher = _launcher(FakeServer([session]), FakePane(session_id="$9"))
    with pytest.raises(TmuxPlacementError, match="session mark refused"):
        launcher.place_chat(thread_id="term_x", argv=["too"], directory="/work")
    assert not session.opened
