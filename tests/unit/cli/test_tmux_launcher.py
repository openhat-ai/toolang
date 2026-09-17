"""Tmux placement: session naming, the launcher operations, and chat dispatch."""

from __future__ import annotations

import os
from typing import Any, cast

import pytest

from toolang.cli.common import tmux
from toolang.cli.toolang.commands.chat import main as chat

TMUX_ENV = {"TMUX": "/tmp/tmux-1/sock,1,0", "TMUX_PANE": "%3"}


class FakePad:
    """The active pane of a fake window, with the identity the protocol needs."""

    def __init__(self, window: FakeWindow) -> None:
        self.pane_id = "%1"
        self.session_id = "$0"
        self.window = window
        self.options: dict[str, str] = {}

    def show_option(self, option: str) -> Any:
        return self.options.get(option)

    def set_option(self, option: str, value: str) -> object:
        self.options[option] = value
        return self

    def unset_option(self, option: str) -> object:
        self.options.pop(option, None)
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
        self.active_pane = FakePad(self)
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
        self, *, start_directory: str | None = None, shell: str | None = None
    ) -> FakePad:
        self.pads.append((start_directory, shell))
        return FakePad(self)


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

    def new_window(
        self,
        window_name: str | None = None,
        *,
        start_directory: str | None = None,
        window_shell: str | None = None,
    ) -> FakeWindow:
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
        del attach
        self.created.append((session_name, start_directory, window_command))
        session = FakeSession(f"${len(self._sessions)}", session_name)
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


class DetachedServer(FakeServer):
    """A server with no attached client: switching cannot find one."""

    def switch_client(self, target_session: str) -> object:
        raise RuntimeError("no current client")


class UnreachableServer(FakeServer):
    """A server whose client can be neither switched nor attached."""

    def switch_client(self, target_session: str) -> object:
        raise RuntimeError("no current client")

    def attach_session(self, target_session: str) -> object:
        raise RuntimeError("not a terminal")


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

    monkeypatch.setattr(chat, "context_layout", lambda _ctx: _Layout())
    monkeypatch.setattr(chat, "resolve_launcher", lambda **_kwargs: launcher)
    return chat._place_chat(
        cast(Any, None),
        thread_id=thread_id,
        argv=["too", "eve", "chat"] if argv is None else argv,
    )


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
            environment={**TMUX_ENV, tmux.MARKS_ENV: "0"},
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


def test_chat_pad_active_reads_the_active_pane_mark() -> None:
    window = FakeWindow("@1")
    launcher = _launcher(
        FakeServer([FakeSession("$0", "eve")]), FakePane(session_id="$9")
    )

    assert launcher.chat_pad_active(window) is False

    window.active_pane.options[tmux.MARK_PAD] = tmux.PAD_CHAT

    assert launcher.chat_pad_active(window) is True


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

    assert window.options[tmux.MARK_THREAD_ID] == "term_x"


def test_open_pad_splits_the_window_with_the_command() -> None:
    window = FakeWindow("@1")
    launcher = _launcher(
        FakeServer([FakeSession("$0", "eve")]), FakePane(session_id="$9")
    )

    assert launcher.open_pad(window, command="too eve chat", directory="/work") is True
    assert window.pads == [("/work", "too eve chat")]


def test_thread_window_picks_the_newest_match() -> None:
    session = FakeSession("$0", "eve")
    older = session.add_window(FakeWindow("@1"))
    older.set_option(tmux.MARK_THREAD_ID, "term_x")
    other = session.add_window(FakeWindow("@2"))
    other.set_option(tmux.MARK_THREAD_ID, "term_y")
    newer = session.add_window(FakeWindow("@3"))
    newer.set_option(tmux.MARK_THREAD_ID, "term_x")
    launcher = _launcher(FakeServer([session]), FakePane(session_id="$0"))

    assert launcher.thread_window(session, "term_x") is newer
    assert launcher.thread_window(session, "term_missing") is None


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

    assert launcher.ensure_session(command="c") == (None, None)


def test_open_window_reports_a_refused_window() -> None:
    class Refusing(FakeSession):
        def new_window(self, *args: Any, **kwargs: Any) -> FakeWindow:
            raise RuntimeError("no space")

    session = Refusing("$0", "eve")
    launcher = _launcher(FakeServer([session]), FakePane(session_id="$9"))

    assert launcher.open_window(session, command="c") is None


def test_switch_client_selects_the_window_then_switches() -> None:
    session = FakeSession("$0", "eve")
    window = session.add_window(FakeWindow("@1"))
    server = FakeServer([session])
    launcher = _launcher(server, FakePane(session_id="$0"))

    assert launcher.switch_client(window) is True
    assert window.selected is True
    assert server.switched == ["eve"]
    assert server.attached == []


def test_switch_client_attaches_when_no_client_is_attached() -> None:
    session = FakeSession("$0", "eve")
    window = session.add_window(FakeWindow("@1"))
    server = DetachedServer([session])
    launcher = _launcher(server, FakePane(session_id="$0"))

    assert launcher.switch_client(window) is True
    assert server.attached == ["eve"]


def test_list_windows_ignores_a_session_that_will_not_list() -> None:
    class Unlistable(FakeSession):
        @property
        def windows(self) -> list[FakeWindow]:
            raise RuntimeError("session gone")

    session = Unlistable("$0", "eve")
    launcher = _launcher(FakeServer([session]), FakePane(session_id="$0"))

    assert launcher.list_windows(session) == ()
    assert launcher.thread_window(session, "term_x") is None


def test_switch_client_reports_an_unreachable_client() -> None:
    session = FakeSession("$0", "eve")
    window = session.add_window(FakeWindow("@1"))
    launcher = _launcher(UnreachableServer([session]), FakePane(session_id="$0"))

    assert launcher.switch_client(window) is False


def test_place_chat_runs_in_place_outside_tmux(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _place(monkeypatch, None) is True
    assert capsys.readouterr().out == ""


def test_place_chat_runs_in_place_in_the_agent_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession("$0", "eve")
    server = FakeServer([session])
    launcher = _launcher(server, FakePane(session_id="$0"))

    assert _place(monkeypatch, launcher) is True
    assert server.switched == []
    assert server.created == []
    assert capsys.readouterr().out == ""


def test_place_chat_switches_to_an_open_thread_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession("$0", "eve")
    window = session.add_window(FakeWindow("@1", "term_x"))
    window.set_option(tmux.MARK_THREAD_ID, "term_x")
    window.active_pane.options[tmux.MARK_PAD] = tmux.PAD_CHAT
    server = FakeServer([session])
    launcher = _launcher(server, FakePane(session_id="$9"))

    assert _place(monkeypatch, launcher, thread_id="term_x") is False
    assert server.created == []
    assert session.opened == []
    assert window.pads == []
    assert window.selected is True
    assert server.switched == ["eve"]
    assert capsys.readouterr().out == "\u21aa opened in tmux session eve\n"


def test_place_chat_opens_a_pad_in_a_container_without_a_live_chat(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession("$0", "eve")
    window = session.add_window(FakeWindow("@1", "term_x"))
    window.set_option(tmux.MARK_THREAD_ID, "term_x")
    server = FakeServer([session])
    launcher = _launcher(server, FakePane(session_id="$9"))

    assert _place(monkeypatch, launcher, thread_id="term_x") is False

    assert window.pads == [(os.getcwd(), "too eve chat")]
    assert server.created == []
    assert session.opened == []
    assert server.switched == ["eve"]
    assert capsys.readouterr().out == "\u21aa opened in tmux session eve\n"


def test_place_chat_labels_a_new_window_for_a_known_thread(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = FakeSession("$0", "eve")
    agent.add_window(FakeWindow("@0"))
    shell = FakeSession("$1", "shell")
    server = FakeServer([shell, agent])
    launcher = _launcher(server, FakePane(session_id="$1"))

    assert (
        _place(
            monkeypatch,
            launcher,
            thread_id="term_x",
            argv=["too", "eve", "chat", "--thread", "term_x"],
        )
        is False
    )

    window = agent.windows[-1]
    assert agent.opened == [(None, os.getcwd(), "too eve chat --thread term_x")]
    assert window.options[tmux.MARK_THREAD_ID] == "term_x"
    assert window.window_name == "term_x"
    assert server.switched == ["eve"]
    assert capsys.readouterr().out == "\u21aa opened in tmux session eve\n"


def test_place_chat_opens_a_window_in_the_agent_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = FakeSession("$0", "eve")
    agent.add_window(FakeWindow("@0"))
    shell = FakeSession("$1", "shell")
    server = FakeServer([shell, agent])
    launcher = _launcher(server, FakePane(session_id="$1"))

    assert (
        _place(monkeypatch, launcher, argv=["too", "eve", "chat", "--thread"]) is False
    )

    assert server.created == []
    assert agent.opened == [(None, os.getcwd(), "too eve chat --thread")]
    assert agent.windows[-1].window_name == tmux.WINDOW_NAME_FALLBACK
    assert server.switched == ["eve"]
    assert capsys.readouterr().out == "\u21aa opened in tmux session eve\n"


def test_place_chat_creates_the_agent_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    shell = FakeSession("$1", "shell")
    server = FakeServer([shell])
    launcher = _launcher(server, FakePane(session_id="$1"))

    assert _place(monkeypatch, launcher) is False

    assert server.created == [("eve", os.getcwd(), "too eve chat")]
    assert server.sessions[-1].windows[0].window_name == tmux.WINDOW_NAME_FALLBACK
    assert server.switched == ["eve"]
    assert capsys.readouterr().out == "\u21aa opened in tmux session eve\n"


def test_place_chat_falls_back_when_a_session_cannot_be_created(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    launcher = _launcher(BrokenServer(), FakePane(session_id="$9"))

    assert _place(monkeypatch, launcher) is True
    assert capsys.readouterr().out == ""


def test_place_chat_keeps_chat_here_when_an_open_thread_cannot_be_reached(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession("$0", "eve")
    window = session.add_window(FakeWindow("@1", "term_x"))
    window.set_option(tmux.MARK_THREAD_ID, "term_x")
    window.active_pane.options[tmux.MARK_PAD] = tmux.PAD_CHAT
    server = UnreachableServer([session])
    launcher = _launcher(server, FakePane(session_id="$9"))

    assert _place(monkeypatch, launcher, thread_id="term_x") is True

    assert server.created == []
    assert session.opened == []
    assert window.selected is True
    assert capsys.readouterr().out == ""


def test_place_chat_closes_its_window_when_the_client_cannot_move(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = FakeSession("$0", "eve")
    agent.add_window(FakeWindow("@0"))
    shell = FakeSession("$1", "shell")
    server = UnreachableServer([shell, agent])
    launcher = _launcher(server, FakePane(session_id="$1"))

    assert _place(monkeypatch, launcher) is True

    assert agent.opened == [(None, os.getcwd(), "too eve chat")]
    assert [window.killed for window in agent.windows] == [False, True]
    assert capsys.readouterr().out == ""
