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
        accent = re.compile(r"^(?:\x1b\[[0-9;]*m)*\x1b\[106m ")
        for columns in (100, 40, 160, 25):
            window.resize(width=columns)
            deadline = time.monotonic() + 10
            lines: list[str] = []
            while time.monotonic() < deadline:
                if rendered_width.exists() and rendered_width.read_text() == str(
                    columns
                ):
                    # after_render flushes output, but tmux may still be parsing
                    # the PTY bytes. Wait for the actual grid, not just the file.
                    lines = pane.capture_pane(escape_sequences=True) or []
                    if (
                        sum(bool(accent.match(line)) for line in lines) == 3
                        and sum("Ask or describe" in line for line in lines) == 1
                    ):
                        break
                time.sleep(0.02)
            else:
                pytest.fail(
                    f"Chat did not redraw at {columns} columns:\n" + "\n".join(lines)
                )
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
        assert "▪︎ run_" in output

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
        session.wait_for("• Thinking", "@lab")

        session.send(b"/flow relay\r")
        running = session.wait_for("flow:relay", "test/scripted")

        assert "flow:relay · test/scripted" not in running
        assert "Traceback" not in running

        (tmp_path / "release-model").touch()
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

        running = session.wait_for("agic:chat", "1s")

        assert "running for" not in running
        assert "■" not in running
        assert "◧" not in running
        (tmp_path / "release-model").touch()
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
        session.wait_for("• Thinking", "@lab")

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
        # Request a complete row before waiting for text that incremental
        # terminal redraws may split across cursor movements.
        _wait_redrawn(session, "space to collapse")
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

        # Exercise collapse, expand, and delete without depending on partial redraw text.
        session.send(b"  d")
        # Deleting the last item restores prompt focus. Wait for typed text to
        # prove the key events were handled before allowing the run to finish.
        session.send(b"back in prompt")
        session.wait_for("back in prompt")
        session.send(b"\x15")
        (tmp_path / "release-model").touch()
        session.wait_for("succeeded", "agic:chat")
        session.send(b"\x04")
        assert session.wait_for_exit() == 0, session.output
        assert "failed" not in session.output
        assert "Traceback" not in session.output
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
        session.wait_for("Thinking")
        session.send(b"queued steer\r")
        session.wait_for("1 queued")
        session.send(b"\t")
        session.wait_for("m-enter steer")
        session.send(b"\x1b\r")
        session.wait_for("1 steer pending")
        session.send(b"second steer\x1b\rthird steer\x1b\r")
        steers = _wait_redrawn(session, "• 3 steers pending")
        assert "second steerthird steer" not in steers
        assert "▮ third steer" in steers
        session.send(b"queued follow-up\r")
        _wait_redrawn(session, "↳ queued follow-up")
        session.send(b"\t")
        output = _wait_redrawn(session, "space to collapse")
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
  context = none
  instruct = none
  user: {{_}}
""",
        responses=[ScriptedModelTurn(result=result, gate=FileGate()), result, result],
    )
    harness.store.close()
    run_chat_tui(harness.setup, harness.state, selects={})


if __name__ == "__main__":
    _run_steer_fixture()


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_chat_tui_displays_real_compaction_lifecycle(tmp_path, outcome):
    mode = "compact-failure" if outcome == "failure" else "compact"
    session = ChatTuiPtySession.start(
        "tests.support.chat_tui_e2e", tmp_path, mode, columns=80
    )
    try:
        session.wait_for("agic:chat", "scripted")
        session.send(b"continue\r")
        running = session.wait_for("Compacting thread history", "1s")
        assert "HIDDEN_COMPACT_SUMMARY" not in running
        assert "compact_read" not in running
        if outcome == "cancel":
            session.send(b"\x03")
            done = session.wait_for("Canceled", "canceled")
        else:
            (tmp_path / "release-compact").touch()
            done = (
                session.wait_for(
                    "Compacted thread history", "VISIBLE_FINAL_ANSWER", "succeeded"
                )
                if outcome == "success"
                else session.wait_for("Failed", "Provider unavailable", "failed")
            )
        assert "HIDDEN_COMPACT_SUMMARY" not in done and "compact_read" not in done
        assert "Traceback" not in done
        if outcome != "success":
            assert "VISIBLE_FINAL_ANSWER" not in done
        (tmp_path / "compact-terminal.txt").write_text(done)
        session.send(b"\x04")
        assert session.wait_for_exit() == 0, session.output
    finally:
        session.close()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
@pytest.mark.parametrize("action", ["delete", "edit", "fifo"])
@pytest.mark.parametrize("terminal_rows", [12, 40])
def test_chat_queue_removal_preserves_input_position_in_terminal(
    tmp_path: Path, action: str, terminal_rows: int
) -> None:
    server = Server(
        socket_name=f"toolang-queue-drain-{uuid4().hex}", config_file=os.devnull
    )
    try:
        session = server.new_session(
            session_name="queue-drain",
            start_directory=PROJECT_ROOT,
            window_command=shlex.join(
                [
                    sys.executable,
                    "-m",
                    "tests.support.chat_tui_e2e",
                    str(tmp_path),
                    "queue",
                ]
            ),
            x=100,
            y=terminal_rows,
            environment={"TOOLANG_TMUX": "0", "TERM": "xterm-256color"},
        )
        pane = session.active_window.active_pane
        assert pane is not None

        def wait_for_layout(
            count: int,
            *,
            started: bool = False,
            completed: int = -1,
            draft: str = "Ask or describe a task",
        ) -> int:
            deadline = time.monotonic() + 10
            lines: list[str] = []
            while time.monotonic() < deadline:
                lines = pane.capture_pane() or []
                inputs = [i for i, line in enumerate(lines) if line.strip() == draft]
                summaries = [
                    line.strip() for line in lines if re.search(r"\d+ queued", line)
                ]
                ready = (
                    len(summaries) == 1 and summaries[0].startswith(f"{count} queued")
                    if count
                    else not summaries
                )
                # Completed output can be above the viewport in short terminals.
                transcript = (
                    pane.cmd("capture-pane", "-p", "-S", "-").stdout
                    if completed >= 0
                    else []
                )
                if (
                    len(inputs) == 1
                    and ready
                    and (not started or any("• Thinking" in line for line in lines))
                    and any("agic:chat" in line for line in lines[inputs[0] + 1 :])
                    and (
                        completed < 0
                        or any(
                            f"queue response {completed}" in line for line in transcript
                        )
                    )
                    and (completed != 3 or not any("Working" in line for line in lines))
                ):
                    return inputs[0]
                time.sleep(0.02)
            pytest.fail("Unexpected queue layout:\n" + "\n".join(lines))

        wait_for_layout(0)
        pane.send_keys("hold queue", enter=True)
        # Wait for the submission to clear Input and the initial live progress
        # to settle before typing another request or measuring its position.
        wait_for_layout(0, started=True)
        for count in range(1, 4):
            pane.send_keys(f"queued request {count}", enter=True)
            wait_for_layout(count)
        previous_row = wait_for_layout(3)
        if action == "fifo":
            for completed in range(4):
                (tmp_path / f"release-model-{completed}").touch()
                row = wait_for_layout(max(0, 2 - completed), completed=completed)
                assert row >= previous_row
                previous_row = row
        else:
            history = pane.cmd("capture-pane", "-p", "-S", "-", "-E", "-1").stdout
            pane.send_keys("Tab", enter=False)
            for remaining in (2, 1, 0):
                pane.send_keys("e" if action == "edit" else "d", enter=False)
                draft = (
                    f"queued request {3 - remaining}"
                    if action == "edit"
                    else "Ask or describe a task"
                )
                assert wait_for_layout(remaining, draft=draft) == previous_row
                assert (
                    pane.cmd("capture-pane", "-p", "-S", "-", "-E", "-1").stdout
                    == history
                )
                if action == "edit":
                    pane.send_keys("C-u", enter=False)
                    assert wait_for_layout(remaining) == previous_row
                    if remaining:
                        pane.send_keys("Tab", enter=False)
    finally:
        server.kill()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
@pytest.mark.parametrize("clear_before_run", [False, True])
def test_chat_run_status_stays_above_queue_and_input_in_terminal(
    tmp_path: Path,
    clear_before_run: bool,
) -> None:
    bootstrap = """
import runpy
import sys
from pathlib import Path
from toolang.cli.toolang.commands.chat.tui import ChatTuiApp

original_init = ChatTuiApp.__init__
def init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    def rendered(app):
        Path(sys.argv[1], 'run-status-width').write_text(str(app.output.get_size().columns))
    self.app.after_render += rendered
ChatTuiApp.__init__ = init
runpy.run_module('tests.support.chat_tui_e2e', run_name='__main__')
"""
    server = Server(
        socket_name=f"toolang-run-status-{uuid4().hex}", config_file=os.devnull
    )
    try:
        session = server.new_session(
            session_name="run-status",
            start_directory=PROJECT_ROOT,
            window_command=shlex.join(
                [
                    sys.executable,
                    "-c",
                    bootstrap,
                    str(tmp_path),
                    "status",
                ]
            ),
            x=100,
            y=30,
            environment={"TOOLANG_TMUX": "0", "TERM": "xterm-256color"},
        )
        window = session.active_window
        pane = window.active_pane
        assert pane is not None

        rendered_width = tmp_path / "run-status-width"

        def wait_for_layout(
            *, queued: bool, running: bool, columns: int = 100, startup: bool = False
        ) -> list[str]:
            deadline = time.monotonic() + 10
            lines: list[str] = []
            while time.monotonic() < deadline:
                # tmux can expose the old frame before Chat handles SIGWINCH.
                # Wait for the app's render, then for the PTY grid to catch up.
                if not rendered_width.exists() or rendered_width.read_text() != str(
                    columns
                ):
                    time.sleep(0.02)
                    continue
                lines = pane.capture_pane() or []
                input_rows = [
                    i for i, line in enumerate(lines) if "Ask or describe" in line
                ]
                queue_rows = [i for i, line in enumerate(lines) if "1 queued" in line]
                session_rows = [
                    line
                    for i, line in enumerate(lines)
                    if input_rows
                    and i > input_rows[0]
                    and "agic:chat" in line
                    and line.rstrip().endswith("· auto")
                ]
                if (
                    len(input_rows) == 1
                    and len(queue_rows) == int(queued)
                    and len(session_rows) == 1
                    and len(session_rows[0].rstrip()) == columns - 2
                ):
                    surface = queue_rows[0] if queued else input_rows[0] - 1
                    if startup:
                        header_bottom = next(
                            i for i, line in enumerate(lines) if line.startswith("╰")
                        )
                        if surface == header_bottom + 2:
                            return lines
                        time.sleep(0.02)
                        continue
                    status = lines[surface - 1] if surface >= 2 else "invalid"
                    elapsed = bool(
                        re.fullmatch(
                            r"  Working for (?:\d+s|\d+m\d+s|\d+h\d+m\d+s)",
                            status.rstrip(),
                        )
                    )
                    if (elapsed if running else not status.strip()) and not lines[
                        surface - 2
                    ].strip():
                        return lines
                time.sleep(0.02)
            pytest.fail("Unexpected terminal layout:\n" + "\n".join(lines))

        def clear_and_wait_for_top_input() -> None:
            pane.send_keys("C-l", enter=False)
            deadline = time.monotonic() + 10
            lines: list[str] = []
            while time.monotonic() < deadline:
                lines = pane.capture_pane() or []
                if (
                    len(lines) >= 4
                    and "Ask or describe" in lines[1]
                    and "agic:chat" in lines[3]
                    and not any("Working" in line for line in lines)
                ):
                    return
                time.sleep(0.02)
            pytest.fail(
                "Input did not move to the top after clear:\n" + "\n".join(lines)
            )

        initial = wait_for_layout(queued=False, running=False, startup=True)
        initial_input_row = next(
            i for i, line in enumerate(initial) if "Ask or describe" in line
        )
        if clear_before_run:
            clear_and_wait_for_top_input()
        pane.send_keys("hold status", enter=True)
        running = wait_for_layout(queued=False, running=True)
        control_row = next(i for i, line in enumerate(running) if "hold status" in line)
        assert control_row == (1 if clear_before_run else initial_input_row)
        for queued in (False, True):
            if queued:
                pane.send_keys("queued follow-up", enter=True)
            for columns in (100, 40, 100):
                window.resize(width=columns)
                lines = wait_for_layout(queued=queued, running=True, columns=columns)
                session_line = next(
                    line for line in reversed(lines) if "agic:chat" in line
                )
                assert not re.search(r"\b(?:Working|\d+s)\b", session_line)
        # This fixture provides one model response. Remove the queued draft
        # before release so exhaustion diagnostics cannot disturb the idle frame.
        pane.send_keys("Tab", enter=False)
        pane.send_keys("d", enter=False)
        wait_for_layout(queued=False, running=True)
        (tmp_path / "release-model").touch()
        wait_for_layout(queued=False, running=False)
        clear_and_wait_for_top_input()
    finally:
        server.kill()
