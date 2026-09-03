"""End-to-end terminal chat coverage through a real pseudo-terminal."""

from __future__ import annotations

import os
from pathlib import Path
import time

import pytest

from tests.support.chat_tui_pty import ChatTuiPtySession

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="pseudo-terminal chat testing requires POSIX",
)


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
            "■ agic:chat",
            "scripted",
        )
        session.send(b"hello from user")
        session.wait_for("hello from user")
        session.send(b"\r")
        output = session.wait_for(
            "hello from user",
            "hello from terminal e2e",
            "succeeded",
        )

        assert "run_" in output
        assert "Traceback" not in output

        exit_started = time.monotonic()
        session.send(b"\x04")
        return_code = session.wait_for_exit()
        assert return_code == 0, session.output
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
            "■ agic:chat",
            "scripted",
        )
        assert "embedded" not in banner

        session.send(b"hello remote\r")
        output = session.wait_for(
            "hello remote",
            "hello from remote e2e",
            "succeeded",
        )
        assert "run_" in output
        assert "Traceback" not in output

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
        session.wait_for("Toolang", "■ agic:chat", "scripted")
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
        session.wait_for("■ agic:chat", "scripted")
        session.send(b"hold status\r")
        session.wait_for("◧ agic:chat running")

        session.send(b"/flow relay\r")
        running = session.wait_for("flow:relay · test/scripted")

        assert "agic:chat" in running
        assert "Traceback" not in running

        session.wait_for("succeeded", "■ flow:relay", "scripted")
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
        session.wait_for("■ agic:chat", "scripted")
        session.send(b"hold queue\r")
        session.wait_for("◧ agic:chat running")

        session.send(b"queued follow-up\r")
        visible = session.wait_for(
            "1 item queued",
            "[1] queued follow-up",
            "Space collapse",
        )
        assert "Traceback" not in visible

        session.send(b"\t")
        focused = session.wait_for(
            "> [1] queued follow-up",
            "1 item queued",
            "E edit",
            "Meta-Enter steer",
            "D/Delete delete",
        )
        assert "Traceback" not in focused

        session.send(b" ")
        session.wait_for("Space expand")
        session.send(b" ")
        session.send(b"d")
        session.wait_for("succeeded", "■ agic:chat")
        session.send(b"\x04")
        assert session.wait_for_exit() == 0, session.output
    finally:
        session.close()
