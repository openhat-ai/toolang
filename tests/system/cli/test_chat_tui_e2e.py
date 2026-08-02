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
        session.wait_for("Toolang", "^d exit")
        session.send(b":flow research\r")
        session.wait_for("Runnable not found: research")
        session.send(b"hello from user")
        session.wait_for("hello from user")
        session.send(b"\r")
        output = session.wait_for(
            "> hello from user",
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


def test_chat_tui_bounds_long_live_output_in_a_small_terminal(
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
        session.wait_for("Toolang", "^d exit")
        session.send(b"show long output\r")
        live_output = session.wait_for(
            "earlier live lines",
            "terminal e2e line 099",
            timeout=10,
        )
        assert "Window too small" not in live_output

        final_output = session.wait_for("succeeded", timeout=10)
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
            "[0] run chat",
            "succeeded",
            "result saved",
            ":show run_",
        )
        assert "1 run" in output
        assert "Window too small" not in output
        show_index = output.rfind(":show run_")
        run_id = output[show_index + len(":show ") :].split()[0]
        assert output.rfind("◇ result saved") < output.rfind(f"◆ {run_id} succeeded")

        session.send(b":show\r")
        result = session.wait_for("Result run_", "hello from terminal e2e")
        assert "Traceback" not in result

        session.send(b"\x04")
        assert session.wait_for_exit() == 0, session.output
    finally:
        session.close()
