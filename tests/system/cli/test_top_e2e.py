"""Top interaction over actual HTTP/SSE and a pseudo-terminal."""

import json
import os
import time

import httpx
import pytest

from tests.support.chat_tui_pty import ChatTuiPtySession

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires a PTY")


def test_top_escape_cancels_editor_without_another_key(tmp_path):
    session = ChatTuiPtySession.start(
        "tests.support.top_tui_e2e", tmp_path, columns=160
    )
    try:
        session.wait_for("View Thread", "1 active")
        session.send(b"\x1b[14~")  # F4
        session.wait_for("Filter:", "Esc cancel")
        session.send(b"\x01")
        session.wait_for("active=True")
        session.data.clear()
        session.send(b"\x1b")
        session.wait_for("F4 Filter", timeout=3)
        session.send(b"\x1b[14~")
        session.data.clear()
        session.wait_for("active=False")
        session.send(b"\x1b")
        session.data.clear()
        session.wait_for("F4 Filter")
        session.send(b"q")
        assert session.wait_for_exit() == 0
    finally:
        session.close()


def test_top_pastes_filter_and_handles_invalid_stats(tmp_path):
    session = ChatTuiPtySession.start(
        "tests.support.top_tui_e2e", tmp_path, columns=160
    )
    try:
        session.wait_for("View Thread", "1 active")
        session.send(b"\x1b[14~\x1b[200~math__double\x1b[201~")
        session.wait_for("Filter: math__double")
        session.send(b"\r")
        session.wait_for("math__double", "F4 Filter")
        session.send(b"\x1b[19~\x15" + b"9" * 24 + b"w\r")  # F8, clear, invalid range
        session.wait_for("Stats start is outside the supported date range")
        session.send(b"\x15all\r")
        session.wait_for("Stats all", "TIME*")
        session.send(b"q")
        assert session.wait_for_exit() == 0
    finally:
        session.close()


@pytest.mark.parametrize("columns", [80, 160])
def test_top_live_tree_views_ranges_and_completion(tmp_path, columns):
    session = ChatTuiPtySession.start(
        "tests.support.top_tui_e2e", tmp_path, columns=columns, rows=24
    )
    try:
        session.wait_for("View Thread", "1 active", "$0.25")
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

            session.send(b"e" + (b">>" if columns == 80 else b""))
            session.wait_for("View Execution", "/ List", root["id"])
            session.send(b"\x1b[15~")
            session.wait_for("/ Tree", current_tool["id"], "└─")
            session.send(b"\x1b[D")
            session.wait_for("[+]")
            session.send(b"\x1b[C\x1b[B\x1b[B\x1b[B\r")
            session.wait_for("Inspect:", current_tool["id"], "Total:")
            session.send(b"\x1b")
            # The same tree selection survives view changes and range refreshes.
            session.send(b"a")
            session.wait_for("View Agent")
            session.send(b"t")
            session.data.clear()
            session.wait_for("View Thread")
            session.send(b"e\x1b[17~")
            session.wait_for("Sort spend")
            session.send(b"\x1b[17~")
            session.wait_for("Sort time")
            session.send(b"\x1b[18~\x15all\r")
            session.wait_for("Recent all")
            session.send(b"\x1b[19~\x15all\r")
            session.wait_for("Stats all", "TIME*")

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
            session.wait_for(model["id"])
            (tmp_path / "release-model").touch()
            session.data.clear()
            session.wait_for("0 active")
            session.send(b"\r")
            session.wait_for("succeeded · flow:review")
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
        session.wait_for("View Thread", "1 active", timeout=15)
        # Begin just after a full frame; a scheduled-only key update would take 5s.
        session.data.clear()
        start = time.monotonic()
        session.send(b"e")
        session.wait_for("View Execution", timeout=1)
        assert time.monotonic() - start < 1
        session.data.clear()
        session.send(b"\x1b[14~")
        session.wait_for("Filter:", timeout=1)
        session.send(b"\x1b")
        session.data.clear()
        session.wait_for("F4 Filter", timeout=1)
        session.send(b"q")
        assert session.wait_for_exit() == 0
    finally:
        session.close()
