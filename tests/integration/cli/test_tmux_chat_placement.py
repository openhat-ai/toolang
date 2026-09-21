"""Chat routing against an isolated tmux server and real control clients."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
import pty
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, cast

import libtmux
import pytest

from toolang.cli.common.tmux import Launcher, MARK_AGENT, MARK_THREAD, MARK_PAD

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is not installed"
)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[libtmux.Server]:
    monkeypatch.setenv("TOOLANG_TMUX", "1")
    # Keep the Unix socket path short, including on macOS's long pytest paths.
    with tempfile.TemporaryDirectory(prefix="toolang-tmux-", dir="/tmp") as directory:
        server = libtmux.Server(
            socket_path=f"{directory}/socket", config_file="/dev/null"
        )
        server.new_session(
            session_name="origin", attach=False, window_command="sleep 60"
        )
        try:
            yield server
        finally:
            tmux(server, "kill-server")


@contextmanager
def control_client(
    server: libtmux.Server, session: libtmux.Session
) -> Iterator[subprocess.Popen[bytes]]:
    with subprocess.Popen(
        [
            "tmux",
            "-S",
            str(server.socket_path),
            "-C",
            "attach-session",
            "-t",
            str(session.session_id),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ) as client:
        try:
            initial = events(client, wait_for_first=True)
            assert "%session-changed" in initial
            yield client
        finally:
            assert client.stdin is not None
            if client.poll() is None:
                client.stdin.write(b"\n")
                client.stdin.flush()
                client.wait(timeout=5)


@contextmanager
def terminal_client(server: libtmux.Server, session: libtmux.Session) -> Iterator[None]:
    master, slave = pty.openpty()
    environment = {
        k: v for k, v in os.environ.items() if k not in {"TMUX", "TMUX_PANE"}
    }
    environment["TERM"] = "xterm-256color"
    try:
        with subprocess.Popen(
            [
                "tmux",
                "-S",
                str(server.socket_path),
                "attach-session",
                "-t",
                str(session.session_id),
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=environment,
        ) as client:
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    modes = tmux(server, "list-clients", "-F", "#{client_control_mode}")
                    if "0" in modes:
                        break
                    assert client.poll() is None
                    time.sleep(0.02)
                else:
                    pytest.fail("ordinary tmux client did not attach")
                yield
            finally:
                if client.poll() is None:
                    client.terminate()
                client.wait(timeout=5)
    finally:
        os.close(slave)
        os.close(master)


def tmux(server: libtmux.Server, *args: str) -> list[str]:
    # libtmux's deprecated cmd alias has an imprecise union annotation.
    result = cast(Any, server).cmd(*args)
    assert not result.stderr, result.stderr
    return result.stdout


def events(client: subprocess.Popen[bytes], *, wait_for_first: bool = False) -> str:
    assert client.stdout is not None
    output = b""
    with selectors.DefaultSelector() as selector:
        selector.register(client.stdout, selectors.EVENT_READ)
        # Wait for a quiet interval, bounded even if a client keeps writing.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            ready = selector.select(
                max(0, deadline - time.monotonic())
                if wait_for_first and not output
                else 0.15
            )
            if not ready:
                break
            chunk = os.read(client.stdout.fileno(), 65536)
            if not chunk:
                break
            output += chunk
    return output.decode()


def test_same_session_selection_uses_session_id_for_a_linked_window(
    server: libtmux.Server,
) -> None:
    origin = server.sessions[0]
    source = origin.active_pane
    assert source is not None
    target = origin.new_window(
        window_name="target", window_shell="sleep 60", attach=False
    )
    other = server.new_session(
        session_name="other", attach=False, window_command="sleep 60"
    )
    tmux(
        server,
        "link-window",
        "-d",
        "-s",
        str(target.window_id),
        "-t",
        f"{other.session_id}:1",
    )
    other_before = other.active_window.window_id
    launcher = Launcher(agent="eve", _server=cast(Any, server), _pane=cast(Any, source))

    with (
        terminal_client(server, origin),
        control_client(server, origin) as first,
        control_client(server, other) as second,
    ):
        events(first)
        clients_before = tmux(
            server, "list-clients", "-F", "#{client_name}:#{session_id}"
        )
        assert launcher.select_target(
            cast(Any, target), pane=cast(Any, target.active_pane)
        )
        assert origin.active_window.window_id == target.window_id
        assert other.active_window.window_id == other_before
        assert (
            tmux(server, "list-clients", "-F", "#{client_name}:#{session_id}")
            == clients_before
        )
        for client in (first, second):
            assert "%session-changed" not in events(client)


@pytest.mark.parametrize(
    "missing,same_session",
    [("session", False), ("window", False), ("pane", False), ("pane", True)],
)
def test_missing_chat_target_starts_once_and_enters_target(
    server: libtmux.Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str,
    same_session: bool,
) -> None:
    origin = server.sessions[0]
    agent = None
    if missing != "session":
        agent = server.new_session(
            session_name="eve", attach=False, window_command="sleep 60"
        )
        agent.set_option(MARK_AGENT, "eve")
        if missing == "pane":
            target = agent.new_window(
                window_name="renamed-thread", window_shell="sleep 60", attach=False
            )
            target.set_option(MARK_THREAD, "term_x")
    source_session = agent if same_session else origin
    assert source_session is not None
    source = source_session.active_pane
    assert source is not None
    before = len(tmux(server, "list-panes", "-a", "-F", "#{pane_id}"))
    monkeypatch.setenv(
        "TMUX", f"{server.socket_path},1,{str(source_session.session_id)[1:]}"
    )
    monkeypatch.setenv("TMUX_PANE", str(source.pane_id))
    result = tmp_path / "child.json"
    child = tmp_path / "child.py"
    child.write_text("""import json, os, sys, time
from pathlib import Path
from types import SimpleNamespace
from toolang.cli.toolang.commands.chat import main as chat
from toolang.cli.common.tmux import resolve_marks
chat.context_layout = lambda ctx: SimpleNamespace(name="eve")
ran = []
chat._chat_interactive = lambda *args, **kwargs: ran.append(True)
chat.chat_command(None)
Path(sys.argv[1] + ".tmp").write_text(json.dumps({"run_here": bool(ran), "pane": os.environ["TMUX_PANE"], "tmux": os.environ["TOOLANG_TMUX"], "marks_disabled": resolve_marks() is None}))
Path(sys.argv[1] + ".tmp").replace(sys.argv[1])
time.sleep(60)
""")
    launcher = Launcher(agent="eve", _server=cast(Any, server), _pane=cast(Any, source))
    with control_client(server, source_session) as client:
        assert not launcher.place_chat(
            directory=str(tmp_path),
            thread_id="term_x",
            argv=[sys.executable, str(child), str(result)],
        )
        deadline = time.monotonic() + 10
        while not result.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert result.exists(), tmux(
            server, "list-panes", "-a", "-F", "#{pane_id}:#{pane_current_command}"
        )
        payload = json.loads(result.read_text())
        assert payload["run_here"] and payload["marks_disabled"]
        assert payload["tmux"] == "0"
        assert len(tmux(server, "list-panes", "-a", "-F", "#{pane_id}")) == before + 1
        target_session = next(
            s
            for s in server.sessions
            if tmux(
                server,
                "display-message",
                "-p",
                "-t",
                str(s.session_id),
                "#{@toolang_agent}",
            )
            == ["eve"]
        )
        target_window = next(
            w
            for w in target_session.windows
            if tmux(
                server,
                "display-message",
                "-p",
                "-t",
                str(w.window_id),
                "#{@toolang_thread}",
            )
            == ["term_x"]
        )
        assert target_session.active_window.window_id == target_window.window_id
        target_pane = target_window.active_pane
        assert target_pane is not None
        assert target_pane.pane_id == payload["pane"]
        assert target_pane.show_option(MARK_PAD) == "chat"
        assert tmux(server, "list-clients", "-F", "#{session_id}") == [
            target_session.session_id
        ]
        assert ("%session-changed" in events(client)) is not same_session
        notice = capsys.readouterr().out.strip()
        assert notice.startswith("created chat pane ")
        address, location = notice.removeprefix("created chat pane ").split(" in ", 1)
        assert location == f"{target_session.session_name}:{target_window.window_name}"
        assert tmux(server, "display-message", "-p", "-t", address, "#{pane_id}") == [
            target_pane.pane_id
        ]


def wait_for_pane_exit(server: libtmux.Server, pane_id: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if tmux(server, "display-message", "-p", "-t", pane_id, "#{pane_dead}") == [
            "1"
        ]:
            return
        time.sleep(0.02)
    pytest.fail(f"pane {pane_id} did not exit")


@pytest.mark.parametrize("missing", ["session", "window", "pane"])
@pytest.mark.parametrize("delay", [0, 0.2])
def test_failed_child_retains_error_and_explicit_retry_reuses_pane(
    server: libtmux.Server,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    missing: str,
    delay: float,
) -> None:
    origin = server.sessions[0]
    source = origin.active_pane
    assert source is not None
    if missing != "session":
        agent = server.new_session(
            session_name="eve", attach=False, window_command="sleep 60"
        )
        agent.set_option(MARK_AGENT, "eve")
        if missing == "pane":
            agent.active_window.rename_window("retained-chat")
            agent.active_window.set_option(MARK_THREAD, "term_x")
    launcher = Launcher(agent="eve", _server=cast(Any, server), _pane=cast(Any, source))
    argv = [
        sys.executable,
        "-c",
        f"import sys,time; time.sleep({delay}); print('chat-startup-failed', flush=True); sys.exit(7)",
    ]
    with control_client(server, origin):
        assert not launcher.place_chat(
            thread_id="term_x", argv=argv, directory=str(tmp_path)
        )
        agent = launcher.agent_session()
        assert agent is not None
        window = launcher.thread_window(agent, "term_x")
        assert window is not None
        pane = next(
            p
            for p in window.panes
            if tmux(server, "display-message", "-p", "-t", p.pane_id, "#{@toolang_pad}")
            == ["chat"]
        )
        wait_for_pane_exit(server, pane.pane_id)
        assert tmux(
            server, "display-message", "-p", "-t", pane.pane_id, "#{pane_dead_status}"
        ) == ["7"]
        assert "chat-startup-failed" in "\n".join(
            tmux(server, "capture-pane", "-p", "-S", "-", "-t", pane.pane_id)
        )
        assert launcher.chat_pad(window) is None
        count = len(tmux(server, "list-panes", "-a"))
        capsys.readouterr()
        assert not launcher.place_chat(
            thread_id="term_x", argv=argv, directory=str(tmp_path)
        )
        wait_for_pane_exit(server, pane.pane_id)
        assert len(tmux(server, "list-panes", "-a")) == count
        assert capsys.readouterr().out == (
            f"reused chat pane {pane.pane_id} in {agent.session_name}:{window.window_name}\n"
        )


def test_successful_child_closes_its_pane(
    server: libtmux.Server, tmp_path: Path
) -> None:
    origin = server.sessions[0]
    source = origin.active_pane
    assert source is not None
    launcher = Launcher(agent="eve", _server=cast(Any, server), _pane=cast(Any, source))
    marker = tmp_path / "exit"
    argv = [
        sys.executable,
        "-c",
        f"import pathlib,time; p=pathlib.Path({str(marker)!r});\nwhile not p.exists(): time.sleep(.02)",
    ]
    with control_client(server, origin) as client:
        assert not launcher.place_chat(
            thread_id="term_x", argv=argv, directory=str(tmp_path)
        )
        marker.touch()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if tmux(server, "list-sessions", "-F", "#{session_name}") == ["origin"]:
                break
            time.sleep(0.02)
        else:
            pytest.fail("successful Chat did not close its pane/session")
        assert client.poll() is None
        assert tmux(server, "list-clients", "-F", "#{session_id}") == [
            origin.session_id
        ]


def test_retention_setup_failure_is_visible_and_does_not_start_chat(
    server: libtmux.Server, tmp_path: Path
) -> None:
    import shlex

    stub = tmp_path / "tmux"
    stub.write_text("#!/bin/sh\necho 'retention refused' >&2\nexit 1\n")
    stub.chmod(0o755)
    marker = tmp_path / "started"
    command = Launcher.chat_command(
        [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ]
    )
    origin = server.sessions[0]
    window = origin.new_window(
        attach=False, window_shell=f"env PATH={shlex.quote(str(tmp_path))} {command}"
    )
    pane = window.active_pane
    assert pane is not None
    deadline = time.monotonic() + 5
    output = ""
    while time.monotonic() < deadline:
        output = "\n".join(tmux(server, "capture-pane", "-p", "-t", str(pane.pane_id)))
        if "press Enter to close" in output:
            break
        time.sleep(0.02)
    assert "retention refused" in output and "press Enter to close" in output
    assert not marker.exists()


def test_cross_session_navigation_switches_only_one_shared_client(
    server: libtmux.Server,
) -> None:
    origin = server.sessions[0]
    source = origin.active_pane
    assert source is not None
    target = server.new_session(
        session_name="eve", attach=False, window_command="sleep 60"
    )
    target.set_option(MARK_AGENT, "eve")
    window = target.active_window
    window.set_option(MARK_THREAD, "term_x")
    pane = window.active_pane
    assert pane is not None
    pane.set_option(MARK_PAD, "chat")
    launcher = Launcher(agent="eve", _server=cast(Any, server), _pane=cast(Any, source))
    with (
        control_client(server, origin) as first,
        control_client(server, origin) as second,
    ):
        assert not launcher.place_chat(
            thread_id="term_x", argv=["must-not-start"], directory="/tmp"
        )
        sessions = tmux(server, "list-clients", "-F", "#{session_id}")
        assert sorted(sessions) == sorted(
            [str(origin.session_id), str(target.session_id)]
        )
        notifications = [events(first), events(second)]
        assert sum("%session-changed" in output for output in notifications) == 1
