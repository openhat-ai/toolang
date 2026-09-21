"""End-to-end terminal chat coverage through a real pseudo-terminal."""

from __future__ import annotations

import os
import re
import signal
from pathlib import Path
import shlex
import shutil
import sys
import time
from uuid import uuid4

from libtmux import Server
import pytest

from tests import PROJECT_ROOT
from tests.support.chat_tui_pty import ChatTuiPtySession

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="pseudo-terminal chat testing requires POSIX",
)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
@pytest.mark.parametrize("refresh", ["resize", "render"])
def test_chat_input_repaints_after_terminal_reflow(
    tmp_path: Path, refresh: str
) -> None:
    # Use an actual terminal grid so the assertion does not duplicate the
    # application's reflow formula. The render path simulates an invalidation
    # being serviced before the resize callback.
    bootstrap = f"""
import runpy
import sys
from pathlib import Path
from toolang.cli.toolang.commands.chat.tui import ChatTuiApp

original_init = ChatTuiApp.__init__
def init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    def rendered(app):
        Path(sys.argv[1], 'rendered-width').write_text(str(app.output.get_size().columns))
    self.app.after_render += rendered
    if {refresh!r} == 'render':
        self.app._on_resize = self.app._redraw
ChatTuiApp.__init__ = init
runpy.run_module('tests.support.chat_tui_e2e', run_name='__main__')
"""
    server = Server(socket_name=f"toolang-resize-{uuid4().hex}", config_file=os.devnull)
    try:
        session = server.new_session(
            session_name="resize",
            start_directory=PROJECT_ROOT,
            window_command=shlex.join([sys.executable, "-c", bootstrap, str(tmp_path)]),
            x=100,
            y=30,
            environment={"TOOLANG_TMUX": "0", "TERM": "xterm-256color"},
        )
        window = session.active_window
        pane = window.active_pane
        assert pane is not None
        rendered_width = tmp_path / "rendered-width"
        for columns in (100, 40, 160, 25):
            window.resize(width=columns)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if rendered_width.exists() and rendered_width.read_text() == str(
                    columns
                ):
                    break
                time.sleep(0.02)
            else:
                pytest.fail(f"Chat did not redraw at {columns} columns")
            lines = pane.capture_pane(escape_sequences=True)
            assert lines is not None
            accent = re.compile(r"^(?:\x1b\[[0-9;]*m)*\x1b\[106m ")
            assert sum(bool(accent.match(line)) for line in lines) == 3, "\n".join(
                lines
            )
            assert sum("Ask or describe" in line for line in lines) == 1, lines
    finally:
        server.kill()


def test_chat_tui_runs_one_local_exchange_in_a_pseudo_terminal(
    tmp_path: Path,
) -> None:
    session = ChatTuiPtySession.start("tests.support.chat_tui_e2e", tmp_path)
    try:
        session.wait_for(
            "Toolang",
            "executor",
            "embedded",
            "Ask or describe a task",
            "agic:chat",
            "scripted",
        )
        session.wait_for_bytes(b"\x1b]0;[new chat]\x07")
        session.send(b"hello from user")
        session.wait_for("hello from user")
        session.send(b"\x1b[O\x1b[I" * 3)
        session.send(b"\r")
        output = session.wait_for(
            "hello from user",
            "hello from terminal e2e",
            "succeeded",
        )

        assert "run_" in output
        assert "Traceback" not in output
        assert "[O[I" not in output
        session.wait_for_bytes(b"\x1b]0;hello from user\x07")

        exit_started = time.monotonic()
        session.send(b"\x04")
        return_code = session.wait_for_exit()
        assert return_code == 0, session.output
        assert b"\x1b]0;\x07" in session.data
        assert time.monotonic() - exit_started < 0.75
    finally:
        session.close()


def test_chat_tui_runs_one_remote_exchange_in_a_pseudo_terminal(
    tmp_path: Path,
) -> None:
    session = ChatTuiPtySession.start("tests.support.chat_tui_remote_e2e", tmp_path)
    try:
        banner = session.wait_for(
            "Toolang",
            "executor",
            ":7001",
            "Ask or describe a task",
            "agic:chat",
            "scripted",
        )
        assert "embedded" not in banner
        session.wait_for_bytes(b"\x1b]0;[new chat]\x07")

        session.send(b"hello remote\r")
        output = session.wait_for(
            "hello remote",
            "hello from remote e2e",
            "succeeded",
        )
        assert "run_" in output
        assert "Traceback" not in output
        session.wait_for_bytes(b"\x1b]0;hello remote\x07")

        session.send(b"\x04")
        assert session.wait_for_exit() == 0, session.output
    finally:
        session.close()


def test_chat_tui_preserves_long_final_output_in_a_small_terminal(
    tmp_path: Path,
) -> None:
    session = ChatTuiPtySession.start(
        "tests.support.chat_tui_e2e",
        tmp_path,
        "long-output",
        rows=12,
        columns=80,
    )
    try:
        session.wait_for("Toolang", "agic:chat", "scripted")
        session.send(b"show long output\r")
        final_output = session.wait_for(
            "• terminal e2e line 000",
            "terminal e2e line 099",
            "succeeded",
            timeout=10,
        )
        assert "terminal e2e line 000" in final_output
        assert "terminal e2e line 099" in final_output
        assert "Window too small" not in final_output

        session.send(b"\x04")
        assert session.wait_for_exit() == 0, session.output
    finally:
        session.close()


def test_chat_tui_reopens_a_durable_flow_result(
    tmp_path: Path,
) -> None:
    session = ChatTuiPtySession.start(
        "tests.support.chat_tui_e2e",
        tmp_path,
        "flow",
    )
    try:
        session.wait_for("Toolang", "flow:relay")
        session.send(b"hello flow\r")
        output = session.wait_for(
            "[0] Run chat",
            "1 run",
            "succeeded",
        )
        assert "Window too small" not in output
        assert "∎ run_" in output

        session.send(b"/output\r")
        result = session.wait_for("• run_", " output ", "hello from terminal e2e")
        assert "Traceback" not in result

        session.send(b"\x04")
        assert session.wait_for_exit() == 0, session.output
    finally:
        session.close()


def test_chat_tui_updates_defaults_while_a_run_is_active(tmp_path: Path) -> None:
    session = ChatTuiPtySession.start(
        "tests.support.chat_tui_e2e",
        tmp_path,
        "status",
    )
    try:
        session.wait_for("agic:chat", "scripted")
        session.send(b"hold status\r")
        session.wait_for("agic:chat running")

        session.send(b"/flow relay\r")
        running = session.wait_for("flow:relay · test/scripted")

        assert "agic:chat" in running
        assert "Traceback" not in running

        session.wait_for("succeeded", "flow:relay", "scripted")
        session.send(b"\x04")
        assert session.wait_for_exit() == 0, session.output
    finally:
        session.close()


def test_chat_tui_status_shows_compact_elapsed_time(tmp_path: Path) -> None:
    session = ChatTuiPtySession.start(
        "tests.support.chat_tui_e2e",
        tmp_path,
        "status",
    )
    try:
        session.wait_for("agic:chat", "scripted")
        session.send(b"hold elapsed status\r")

        running = session.wait_for("agic:chat running", " for 1s")

        assert "■" not in running
        assert "◧" not in running
        session.wait_for("succeeded", "agic:chat")
        session.send(b"\x04")
        assert session.wait_for_exit() == 0, session.output
    finally:
        session.close()


def test_chat_tui_switches_focus_and_deletes_an_active_run_queue_item(
    tmp_path: Path,
) -> None:
    session = ChatTuiPtySession.start(
        "tests.support.chat_tui_e2e",
        tmp_path,
        "status",
    )
    try:
        session.wait_for("agic:chat", "scripted")
        session.send(b"hold queue\r")
        session.wait_for("agic:chat running")

        session.send(b"queued follow-up\r")
        visible = session.wait_for(
            "1 queued",
            "↳ queued follow-up",
            "tab to focus",
        )
        assert "Traceback" not in visible
        assert "expand/collapse" not in visible
        assert "e edit" not in visible

        session.send(b"\t")
        focused = session.wait_for(
            "↳ queued follow-up",
            "1 queued",
            "space to collapse",
            "e edit",
            "m-enter steer",
            "d delete",
        )
        assert "Traceback" not in focused
        assert "m-enter steer · e edit · d delete" in focused
        # Prompt Toolkit redraws only changed cells; force a full redraw to
        # read the summary and its inline hint as one contiguous row.
        redrawn = _wait_redrawn(session, "1 queued (space to collapse)")
        assert "1 queued (space to collapse)" in redrawn

        # Exercise collapse, expand, and delete without depending on partial redraw text.
        session.send(b"  d")
        session.wait_for("succeeded", "agic:chat")
        session.send(b"\x04")
        assert session.wait_for_exit() == 0, session.output
    finally:
        session.close()


def test_chat_tui_keeps_multiple_steers_visible_until_their_step_finishes(
    tmp_path: Path,
) -> None:
    session = ChatTuiPtySession.start(
        "tests.system.cli.test_chat_tui_e2e", tmp_path, rows=12, columns=80
    )
    try:
        session.wait_for("Toolang", "Ask or describe a task")
        session.send(b"start run\r")
        session.wait_for("Thinking...")
        session.send(b"queued steer\r")
        session.wait_for("1 queued")
        session.send(b"\t")
        session.wait_for("m-enter steer")
        session.send(b"\x1b\r")
        session.wait_for("will apply after the current step")
        session.send(b"second steer\x1b\rthird steer\x1b\r")
        steers = _wait_redrawn(session, "• 3 steers will apply after the current step")
        assert "second steerthird steer" not in steers
        assert "  third steer" in steers
        session.send(b"queued follow-up\r")
        _wait_redrawn(session, "↳ queued follow-up")
        session.send(b"\t")
        output = _wait_redrawn(session, "(space to collapse)")
        assert "queued follow-up" in output
        assert "space to collapse" in output
        assert "Window too small" not in output
        (tmp_path / "release-model").touch()
        output = session.wait_for("succeeded")
        assert "Traceback" not in output
        assert "steers applied" not in output
        session.send(b"\x11")
        assert session.wait_for_exit() == 0, session.output
    finally:
        session.close()


def _wait_redrawn(session: ChatTuiPtySession, value: str) -> str:
    # Prompt Toolkit ordinarily writes only changed cells. Request a full redraw
    # so assertions can read a complete row from the PTY byte stream.
    deadline = time.monotonic() + 10
    while value not in session.output and time.monotonic() < deadline:
        session.process.send_signal(signal.SIGWINCH)
        session._read(timeout=0.1)
        time.sleep(0.02)
    return session.wait_for(value, timeout=0.1)


def _run_steer_fixture() -> None:
    """Hold the first model call until the parent PTY test releases it."""
    import asyncio
    import sys

    from tests.support.chat_tui_runner import run_chat_tui
    from tests.support.execution_harness import (
        AsyncGate,
        ExecutionHarness,
        ScriptedModelTurn,
    )
    from toolang.base.types.message import Message
    from toolang.base.types.run import ModelCallResult

    root = Path(sys.argv[1])

    class FileGate(AsyncGate):
        async def wait(self) -> None:
            while not (root / "release-model").exists():
                await asyncio.sleep(0.01)

    result = ModelCallResult(message=Message.assistant("steered response"))
    harness = ExecutionHarness.create(
        root,
        source="""
agic chat(_: Part[]) -> Part[]:
  recall = none
  context: none
  instruct: none
  user: {{_}}
""",
        responses=[ScriptedModelTurn(result=result, gate=FileGate()), result, result],
    )
    harness.store.close()
    run_chat_tui(harness.setup, harness.state, selects={})


if __name__ == "__main__":
    _run_steer_fixture()
