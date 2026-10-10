"""Top interaction over actual HTTP/SSE and a pseudo-terminal."""

from contextlib import closing
import json
import os
import re
import shlex
import shutil
import sys
import time
from uuid import uuid4

import httpx
from libtmux import Server
import pytest

from tests import PROJECT_ROOT
from tests.support.chat_tui_pty import ChatTuiPtySession

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires a PTY")


def test_top_captures_wheel_in_alternate_screen_and_restores_terminal(tmp_path):
    session = ChatTuiPtySession.start(
        "tests.support.top_tui_e2e", tmp_path, columns=180
    )
    try:
        session.wait_for("THREAD", "1 active")
        session.wait_for_bytes(b"\x1b[?1049h")
        session.wait_for_bytes(b"\x1b[?1000h", timeout=1)
        session.wait_for_bytes(b"\x1b[?1006h", timeout=1)
        session.send(b"e\r")
        session.wait_for("Status: running")
        session.data.clear()
        session.send(b"\x1b[<65;10;10M")
        session.wait_for("Status: succeeded", timeout=1)
        session.data.clear()
        session.send(b"\x1b[<64;10;10M")
        session.wait_for("Status: running", timeout=1)
        session.data.clear()
        session.send(b"\x1b[Ma**")  # Legacy X10 wheel down.
        session.wait_for("Status: succeeded", timeout=1)
        session.send(b"\x1b[21~")  # F10
        assert session.wait_for_exit() == 0
        session.wait_for_bytes(b"\x1b[?1000l")
        session.wait_for_bytes(b"\x1b[?1006l")
        session.wait_for_bytes(b"\x1b[?1049l")
    finally:
        session.close()


def test_top_escape_cancels_editor_without_another_key(tmp_path):
    session = ChatTuiPtySession.start(
        "tests.support.top_tui_e2e", tmp_path, columns=160
    )
    try:
        session.wait_for("THREAD", "1 active")
        session.send(b"\x1b[14~")  # F4
        session.wait_for("Filter:", "Active: False")
        session.send(b"\x01")
        session.wait_for("Active: True")
        session.data.clear()
        session.send(b"\x1b")
        session.wait_for("F4Filter", timeout=3)
        session.send(b"\x1b[14~")
        session.data.clear()
        session.wait_for("Active: False")
        session.send(b"\x1b")
        session.data.clear()
        session.wait_for("F4Filter")
        session.send(b"q")
        assert session.wait_for_exit() == 0
    finally:
        session.close()


def test_top_pastes_filter_and_cycles_stats(tmp_path):
    session = ChatTuiPtySession.start(
        "tests.support.top_tui_e2e", tmp_path, columns=160
    )
    try:
        session.wait_for("THREAD", "1 active")
        session.send(b"\x1b[14~\x1b[200~math__double\x1b[201~")
        session.wait_for("Filter: math__double")
        session.send(b"\r")
        session.wait_for("math__double", "F4Filter")
        session.send(b"\x1b[19~")  # F8 applies the next preset immediately.
        session.wait_for("Stats: 1h", "TIME+")
        session.send(b"\x1b[19~" * 3)
        session.wait_for("Stats: all")
        session.send(b"q")
        assert session.wait_for_exit() == 0
    finally:
        session.close()


@pytest.mark.parametrize("columns", [80, 160])
@pytest.mark.parametrize("local", [False, True])
def test_top_live_tree_views_ranges_and_completion(
    tmp_path, columns, local, monkeypatch
):
    monkeypatch.setenv("TOOLANG_TEST_LOCAL", "1" if local else "0")
    session = ChatTuiPtySession.start(
        "tests.support.top_tui_e2e", tmp_path, columns=columns, rows=24
    )
    try:
        session.wait_for("THREAD", "1 active", "$0.25")
        endpoint = json.loads((tmp_path / "activity-endpoint.json").read_text())[
            "endpoint"
        ]
        with httpx.Client(base_url=endpoint, trust_env=False) as http:

            def snapshot(**params):
                response = http.get("/api/v1/activity/batch", params=params)
                response.raise_for_status()
                return response.json()[0]

            initial = snapshot()
            root = next(n for n in initial["roots"] if n["status"] == "running")
            current_tool = next(
                n for n in initial["paths"] if n["title"] == "math__double"
            )
            assert initial["stats"]["model"] == 2
            assert initial["stats"]["tool"] == 1
            assert initial["stats"]["cost"] == 0.25
            assert current_tool["root"] == root["id"]
            assert current_tool["summary"]
            for params, expected in [
                ({"since": "all", "all_recent": "true"}, 2),
                ({"since": "2099-01-01T00:00:00Z"}, 2),
                ({"filter": "MATH__DOUBLE"}, 1),
                ({"filter": "Already complete"}, 1),
                ({"active": "true"}, 1),
                ({"filter": "no-such-work"}, 0),
            ]:
                page = snapshot(**params)
                assert len(page["roots"]) == expected
                assert page["matched"] == expected
                if "2099" in params.get("since", ""):
                    assert page["stats"]["model"] == 0
                    assert page["stats"]["cost"] == 0

            session.send(b"e\r")
            session.wait_for("Run:", root["id"], "Inspect:")
            session.send(b"\x1b")
            session.send(b"\x1b[15~")
            if columns == 160:
                session.wait_for("STEP", current_tool["id"], "└─")
            session.send(b"\x1b[D")
            if columns == 160:
                session.wait_for("[+]")
            session.send(b"\x1b[C\x1b[B\x1b[B\x1b[B\r")
            session.wait_for("Inspect:", current_tool["id"], "Total:")
            session.send(b"\x1b")
            # The same tree selection survives view changes and range refreshes.
            session.send(b"a")
            session.wait_for("ACTIVITY(30m)")
            session.send(b"t")
            session.data.clear()
            session.wait_for("THREAD")
            session.send(b"e\x1b[17~")
            session.wait_for("SPEND↓")
            session.send(b"\x1b[17~")
            session.wait_for("TIME+↓")
            session.send(b"\x1b[18~" * 4)
            session.wait_for("Activity: all")
            session.send(b"\x1b[19~" * 4)
            session.wait_for("Stats: all", "TIME+")

            (tmp_path / "release-tool").touch()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                page = snapshot()
                model = next(
                    (n for n in page["paths"] if "scripted" in n["title"]), None
                )
                if model:
                    break
                session._read(timeout=0.02)
            else:
                pytest.fail("Tool completion did not expose the current model step")
            assert model["summary"].startswith("preview:")
            assert current_tool["id"] not in {n["id"] for n in page["paths"]}
            if columns == 160:
                session.wait_for(model["id"])
            (tmp_path / "release-model").touch()
            session.data.clear()
            session.send(b"t")
            session.wait_for("idle")
            session.send(b"e\r")
            session.wait_for("Status: succeeded", "flow:review")
            final = snapshot()
            assert not final["paths"]
            assert final["stats"]["model"] == 3
            assert final["stats"]["tool"] == 1
            assert final["stats"]["cost"] == 0.25
            assert all(n["status"] == "succeeded" for n in final["roots"])
        session.send(b"q")
        assert session.wait_for_exit() == 0
    finally:
        session.close()


def test_keys_repaint_without_waiting_for_long_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLANG_TEST_REFRESH", "5")
    session = ChatTuiPtySession.start(
        "tests.support.top_tui_e2e", tmp_path, columns=180
    )
    try:
        session.wait_for("THREAD", "1 active", timeout=15)
        # Begin just after a full frame; a scheduled-only key update would take 5s.
        session.data.clear()
        start = time.monotonic()
        session.send(b"e")
        session.wait_for("RUN", timeout=1)
        assert time.monotonic() - start < 1
        session.data.clear()
        session.send(b"\x1b[14~")
        session.wait_for("Filter:", timeout=1)
        session.send(b"\x1b")
        session.data.clear()
        session.wait_for("F4Filter", timeout=1)
        session.send(b"q")
        assert session.wait_for_exit() == 0
    finally:
        session.close()


@pytest.mark.parametrize("local", [False, True])
def test_ctrl_selection_during_slow_details_and_terminal_restore(
    tmp_path, monkeypatch, local
):
    monkeypatch.setenv("TOOLANG_TEST_LOCAL", "1" if local else "0")
    monkeypatch.setenv("TOOLANG_TEST_SLOW_RESULT", "1")
    session = ChatTuiPtySession.start(
        "tests.support.top_tui_e2e", tmp_path, columns=180
    )
    try:
        session.wait_for("THREAD", "1 active")
        session.wait_for_bytes(b"\x1b[?1049h")
        endpoint = json.loads((tmp_path / "activity-endpoint.json").read_text())[
            "endpoint"
        ]
        with httpx.Client(base_url=endpoint, trust_env=False) as http:
            roots = http.get("/api/v1/activity/batch").json()[0]["roots"]
        running = next(node["id"] for node in roots if node["status"] == "running")
        done = next(node["id"] for node in roots if node["status"] == "succeeded")
        session.send(b"e\r")
        session.wait_for("Inspect: too alice inspect " + running)
        session.data.clear()
        started = time.monotonic()
        session.send(b"\x0e")
        session.wait_for("Inspect: too alice inspect " + done, timeout=1)
        assert time.monotonic() - started < 1
        session.data.clear()
        session.send(b"\x10")
        session.wait_for("Inspect: too alice inspect " + running, timeout=1)
        session.send(b"q")
        assert session.wait_for_exit() == 0
        session.wait_for_bytes(b"\x1b[?1049l")
    finally:
        session.close()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
@pytest.mark.parametrize("local", [False, True])
def test_top_terminal_grid_resize_and_markdown_pages(tmp_path, local):
    """Inspect the current screen, including reflow and Markdown beyond page one."""
    result = (
        "# Review result\n\n**Verified** configuration.\n\n"
        + "\n".join(f"- Item {i}: 中文 e\u0301" for i in range(30))
        + "\n\n```python\nanswer = 42\n```\n\nFinished."
    )
    server = Server(
        socket_name=f"toolang-top-grid-{uuid4().hex}", config_file=os.devnull
    )
    try:
        session = server.new_session(
            session_name="top",
            start_directory=PROJECT_ROOT,
            window_command=shlex.join(
                [sys.executable, "-m", "tests.support.top_tui_e2e", str(tmp_path)]
            ),
            x=160,
            y=30,
            environment={
                "TERM": "xterm-256color",
                "TOOLANG_TEST_LOCAL": "1" if local else "0",
                "TOOLANG_TEST_RESULT": result,
            },
        )
        window = session.active_window
        pane = window.active_pane
        assert pane is not None

        def screen(predicate, timeout=10):
            deadline = time.monotonic() + timeout
            lines = []
            while time.monotonic() < deadline:
                lines = pane.capture_pane() or []
                if predicate(lines):
                    return lines
                time.sleep(0.02)
            pytest.fail("Top did not redraw:\n" + "\n".join(lines))

        screen(lambda lines: "1 active" in "\n".join(lines))
        flags = (
            "#{alternate_on} #{mouse_standard_flag} #{mouse_sgr_flag} #{history_size}"
        )
        assert pane.display_message(flags, get_text=True) == ["1 1 1 0"]
        for width, height in ((160, 30), (80, 24), (40, 15), (160, 30)):
            window.resize(width=width, height=height)
            lines = screen(
                lambda lines: (
                    len(lines) == height
                    and lines[0].startswith("Agent alice ")
                    and len(lines[0]) == width
                    and re.search(r"\d{2}:\d{2}:\d{2}$", lines[0])
                    and "F1Help" in lines[-1]
                    and "F10Quit" in lines[-1]
                    and sum(line.startswith("S ") for line in lines) == 1
                    and sum(line.startswith("+ ") for line in lines) == 1
                )
            )
            assert "…" not in "\n".join(lines)
            heading_index = next(
                i for i, line in enumerate(lines) if line.startswith("S ")
            )
            assert not lines[heading_index - 1].strip()
            # Numeric headings and values share their right edge.
            heading = next(line for line in lines if line.startswith("S "))
            row = next(line for line in lines if line.startswith("+ "))
            model_end = heading.index("MODEL") + len("MODEL")
            assert row[model_end - 1 : model_end] == "2"
            if "SPEND" in heading:
                spend_end = heading.index("SPEND") + len("SPEND")
                assert row[spend_end - 5 : spend_end] == "$0.25"
        assert pane.display_message(flags, get_text=True) == ["1 1 1 0"]

        pane.send_keys("e", enter=False)
        screen(lambda lines: any(" RUN " in line for line in lines))
        pane.send_keys("C-n", enter=False)
        pane.send_keys("Enter", enter=False)
        lines = screen(
            lambda lines: any(
                (match := re.match(r"Details 1-\d+/(\d+)", line)) and int(match[1]) > 30
                for line in lines
            )
        )
        assert any("Review result" in line for line in lines)
        assert "**Verified**" not in "\n".join(lines)
        assert any("Verified configuration." in line for line in lines)
        pane.send_keys("PageDown", enter=False)
        lines = screen(
            lambda lines: any("Item 0: 中文 e\u0301" in line for line in lines)
        )
        # Paging moves only Details; the selected run and table stay in place.
        table = [line[line.index("term_") :] for line in lines if line.startswith("+ ")]
        for _ in range(12):
            pane.send_keys("PageDown", enter=False)
        lines = screen(lambda lines: any("answer = 42" in line for line in lines))
        assert any("Finished." in line for line in lines)
        assert "F1Help" in lines[-1]
        assert [
            line[line.index("term_") :] for line in lines if line.startswith("+ ")
        ] == table
        assert not any("```" in line for line in lines)
        pane.send_keys("C-p", enter=False)
        lines = screen(lambda lines: any("Status: running" in line for line in lines))
        assert any(line.startswith("Details 1-") for line in lines)
        pane.send_keys("Escape", enter=False)
        screen(lambda lines: not any(line.startswith("Details ") for line in lines))
    finally:
        server.kill()


@pytest.mark.parametrize("name", ["alice", "team"])
def test_offline_agent_top_recovers_when_history_becomes_available(tmp_path, name):
    from toolang.common.layout import AgentLayout
    from toolang.execution import statistics
    from toolang.execution.store import RunStore
    from toolang.execution.types import Output
    from tests.support.execution_fixtures import project_run_end
    from tests.unit.execution.test_activity import at, model, root

    layout = AgentLayout.resident(tmp_path, name)
    layout.home.mkdir(parents=True)
    layout.program.write_text("agic main(_: Text) -> Text:\n  user: {{_}}\n")
    session = ChatTuiPtySession.start(
        "toolang.cli.toolang.main",
        "--root",
        tmp_path,
        name,
        "top",
        "--since",
        "all",
        "--recent",
        "all",
        "--view",
        "execution",
        columns=180,
    )
    try:
        session.wait_for(f"Agent {name}  offline", "No matching activity")
        assert not layout.run_store.exists(), "Observation must not create history"
        # Simulate committed history arriving while this read-only client stays open.
        with closing(RunStore(layout.run_store)) as store:
            statistics.start_session(store, "one", at(0))
            root(store)
            model(store)
            project_run_end(
                store,
                run_id="run_root",
                finished_at=at(120),
                output=Output("# Saved result", "_"),
            )
            statistics.checkpoint(store, "one", at(120), end=True)
        session.data.clear()
        session.wait_for(f"Agent {name}  offline  2m00s", "$0.50", "run_root")
        session.send(b"\r")
        session.wait_for("Saved result", f"Inspect: too {name} inspect run_root")
        session.send(b"q")
        assert session.wait_for_exit() == 0
    finally:
        session.close()
