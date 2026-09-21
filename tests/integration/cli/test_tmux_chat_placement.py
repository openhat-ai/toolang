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
from types import SimpleNamespace
from typing import Any, cast

import libtmux
import pytest

from toolang.cli.common.tmux import Launcher, MARK_AGENT, MARK_THREAD
from toolang.cli.toolang.commands.chat import main as chat

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
        assert not launcher.select_target(cast(Any, other.active_window))
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
def test_missing_chat_target_starts_once_without_client_switch(
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
    source_before = (source_session.active_window.window_id, source.pane_id)
    agent_before = agent.active_window.window_id if agent else None
    before = len(tmux(server, "list-panes", "-a", "-F", "#{pane_id}"))
    monkeypatch.setenv(
        "TMUX", f"{server.socket_path},1,{str(source_session.session_id)[1:]}"
    )
    monkeypatch.setenv("TMUX_PANE", str(source.pane_id))
    monkeypatch.setattr(chat.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(chat.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        chat, "context_layout", lambda _ctx: SimpleNamespace(name="eve")
    )
    result = tmp_path / "child.json"
    child = tmp_path / "child.py"
    child.write_text("""import json, os, sys, time
from pathlib import Path
from types import SimpleNamespace
from toolang.cli.toolang.commands.chat import main as chat
chat.context_layout = lambda ctx: SimpleNamespace(name="eve")
run_here = chat._place_chat(None, thread_id="term_x", argv=[sys.executable, *sys.argv])
Path(sys.argv[1] + ".tmp").write_text(json.dumps({"run_here": run_here, "pane": os.environ["TMUX_PANE"], "hint_removed": chat._PLACED_PANE_ENV not in os.environ}))
Path(sys.argv[1] + ".tmp").replace(sys.argv[1])
time.sleep(60)
""")
    with control_client(server, source_session) as client:
        clients_before = tmux(
            server, "list-clients", "-F", "#{client_name}:#{session_id}"
        )
        assert not chat._place_chat(
            cast(Any, None),
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
        assert payload["run_here"] and payload["hint_removed"]
        assert len(tmux(server, "list-panes", "-a", "-F", "#{pane_id}")) == before + 1
        assert (
            tmux(server, "list-clients", "-F", "#{client_name}:#{session_id}")
            == clients_before
        )
        assert "%session-changed" not in events(client)
        if same_session:
            assert source_session.active_pane is not None
            assert source_session.active_pane.pane_id == payload["pane"]
        else:
            assert source_session.active_pane is not None
            assert (
                source_session.active_window.window_id,
                source_session.active_pane.pane_id,
            ) == source_before
            if agent is not None:
                assert agent.active_window.window_id == agent_before
        assert "created chat pane" in capsys.readouterr().out
