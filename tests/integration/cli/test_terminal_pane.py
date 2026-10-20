"""OSC and metadata against a private tmux server, never the user's server."""

from collections.abc import Iterator
import shlex
import shutil
import sys
import tempfile
import time

import libtmux
import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is not installed"
)


@pytest.fixture
def server() -> Iterator[libtmux.Server]:
    with tempfile.TemporaryDirectory(prefix="too-pane-", dir="/tmp") as directory:
        server = libtmux.Server(
            socket_path=f"{directory}/socket", config_file="/dev/null"
        )
        server.new_session(
            session_name="manual",
            window_name="manual-window",
            attach=False,
            window_command="sleep 60",
        )
        try:
            yield server
        finally:
            server.kill()


def wait_for(check):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    assert check()


@pytest.mark.parametrize(
    "kind,label,agent",
    [
        ("chat", "alice>", "alice"),
        ("chat", "alice> topic", "alice"),
        ("talk", "#dev", ""),
        ("top", "alice top", "alice"),
        ("top", "top", ""),
        ("serve", "serve alice", "alice"),
        ("serve", "serve hub", ""),
        ("run", "run file.too build", "file"),
        ("retry", "retry run_source", "alice"),
        ("rerun", "rerun run_source", "alice"),
    ],
)
def test_second_pane_publishes_local_marks_without_changing_containers(
    server, tmp_path, kind, label, agent
):
    window = server.sessions[0].active_window
    original = window.panes[0]
    server.sessions[0].set_option("@toolang_agent", "legacy-agent")
    window.set_option("@toolang_thread", "legacy-thread")
    window.set_option("allow-set-title", "on")
    window.set_option("automatic-rename", "off")
    ready, release, closed = (
        tmp_path / name for name in ("ready", "release", "closed")
    )
    code = f"""
from pathlib import Path
import time
from toolang.cli.common.terminal_session import prepare_terminal
from toolang.common.layout import AgentLayout
root = Path({str(tmp_path)!r})
with prepare_terminal({kind!r}, root=root,
    layout=AgentLayout.resident(root, {agent!r}) if {agent!r} else None,
    label={label!r}, convo="gc_123" if {kind!r} == "talk" else "",
    context="context" if {kind!r} == "talk" or ({kind!r} == "top" and not {agent!r}) else "") as terminal:
    if {kind!r} == "chat": terminal.set_thread("term_full")
    if {kind!r} in {{"run", "retry", "rerun"}}: terminal.set_run("run_actual")
    Path({str(ready)!r}).touch()
    while not Path({str(release)!r}).exists(): time.sleep(.01)
Path({str(closed)!r}).touch()
time.sleep(60)
"""
    pane = window.split(
        shell=shlex.join(
            [
                "env",
                "TOOLANG_TMUX=1",
                f"TOOLANG_TITLE_PREFIX_{kind.upper()}=",
                sys.executable,
                "-c",
                code,
            ]
        ),
        attach=False,
    )

    def option(name):
        return "\n".join(
            server.cmd(
                "show-options", "-p", "-qv", "-t", pane.pane_id, f"@toolang_{name}"
            ).stdout
        )

    def title():
        return "\n".join(
            server.cmd(
                "display-message", "-p", "-t", pane.pane_id, "#{pane_title}"
            ).stdout
        )

    wait_for(lambda: ready.exists() and option("kind") == kind)
    expected_identity = (
        "term_full"
        if kind == "chat"
        else "run_actual"
        if kind in {"run", "retry", "rerun"}
        else None
    )
    if expected_identity:
        wait_for(
            lambda: option("thread" if kind == "chat" else "run") == expected_identity
        )
    assert title() == label
    assert option("root") == str(tmp_path.resolve())
    assert option("agent") == agent
    if kind != "chat":
        assert option("thread") == ""  # Local empty value masks inherited identity.
    assert option("convo") == ("gc_123" if kind == "talk" else "")
    assert len(server.sessions) == len(server.sessions[0].windows) == 1
    assert len(window.panes) == 2 and window.active_pane.pane_id == original.pane_id
    assert (
        window.window_name == "manual-window"
        and server.sessions[0].session_name == "manual"
    )
    assert window.show_option("allow-set-title") is True
    assert window.show_option("automatic-rename") is False
    # libtmux keeps targeting the captured pane even after a user moves it.
    destination = server.new_session(
        session_name="other",
        window_name="other-window",
        attach=False,
        window_command="sleep 60",
    )
    server.cmd(
        "join-pane",
        "-d",
        "-s",
        pane.pane_id,
        "-t",
        destination.active_window.panes[0].pane_id,
    )
    assert option("kind") == kind and title() == label
    assert destination.active_window.window_name == "other-window"
    assert not server.cmd("show-options", "-p", "-t", original.pane_id).stdout
    release.touch()
    wait_for(closed.exists)
    wait_for(lambda: not server.cmd("show-options", "-p", "-t", pane.pane_id).stdout)
    assert title() == ""
    assert window.show_option("@toolang_thread") == "legacy-thread"
    assert server.sessions[0].show_option("@toolang_agent") == "legacy-agent"
