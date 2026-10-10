"""Real-terminal resize checks shared by Chat and Talk."""

import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import time
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from libtmux import Server
import pytest

from tests import PROJECT_ROOT


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
@pytest.mark.parametrize("surface", ["chat", "talk"])
@pytest.mark.parametrize(
    "draft,inputbox_max_width",
    [("", None), ("draft" + " " * 80, 60), ("中文" * 30, 160)],
    ids=["empty-default-width", "trailing-spaces-narrow-input", "wide-text-wide-input"],
)
@pytest.mark.parametrize(
    "resize_height", [False, True], ids=["width", "width-and-height"]
)
def test_repeated_resize_preserves_history_and_input_origin(
    tmp_path, surface, draft, resize_height, inputbox_max_width
):
    server = Server(socket_name=f"toolang-resize-{uuid4().hex}", config_file=os.devnull)
    try:
        session = server.new_session(
            session_name="resize",
            start_directory=PROJECT_ROOT,
            window_command=shlex.join(
                [
                    sys.executable,
                    "-m",
                    "tests.system.cli.test_scrollback_resize",
                    surface,
                    str(tmp_path),
                    draft,
                    str(inputbox_max_width or 0),
                ]
            ),
            x=200,
            y=24,
            environment={"TOOLANG_TMUX": "0", "TERM": "xterm-256color"},
        )
        window = session.active_window
        pane = window.active_pane
        assert pane is not None

        def snapshot(width, height=24, expected_draft=draft, expected_input_row=None):
            deadline = time.monotonic() + 10
            previous = None
            stable = 0
            while time.monotonic() < deadline:
                state_path = tmp_path / "rendered.json"
                if state_path.exists():
                    state = json.loads(state_path.read_text())
                    if (
                        state["width"] == width
                        and state["height"] == height
                        and state["draft"] == expected_draft
                        and not state["waiting_for_cpr"]
                    ):
                        lines = pane.capture_pane() or []
                        placeholder = (
                            "Describe your task"
                            if surface == "chat"
                            else "Type a message"
                        )
                        input_rows = (
                            [i for i, line in enumerate(lines) if placeholder in line]
                            if not expected_draft
                            else [
                                int(
                                    pane.display_message("#{cursor_y}", get_text=True)[
                                        0
                                    ]
                                )
                            ]
                        )
                        assert state["cursor"] == len(expected_draft)
                        flags = pane.display_message(
                            "#{cursor_y} #{cursor_x}", get_text=True
                        )
                        current = (flags, input_rows)
                        stable = stable + 1 if current == previous else 0
                        if (
                            len(input_rows) == 1
                            and stable >= 3
                            and (
                                expected_input_row is None
                                or input_rows == [expected_input_row]
                            )
                        ):
                            return current
                        previous = current
                time.sleep(0.03)
            pytest.fail(
                "Terminal did not settle:\n" + "\n".join(pane.capture_pane() or [])
            )

        initial = snapshot(200)
        for _ in range(5):
            for width, height in (
                ((120, 16), (40, 12), (200, 32), (80, 24), (200, 24))
                if resize_height
                else ((160, 24), (120, 24), (80, 24), (40, 24), (25, 24), (200, 24))
            ):
                window.resize(width=width, height=height)
                current = snapshot(width, height)
            assert current == initial
        if draft:
            pane.send_keys("C-u", enter=False)
            cleared = snapshot(200, expected_draft="")
            for width in (25, 80, 200):
                window.resize(width=width)
                current = snapshot(width, expected_draft="")
            assert current == cleared
        pane.send_keys("C-l", enter=False)
        snapshot(200, expected_draft="", expected_input_row=1)
        for width in (40, 120, 200):
            window.resize(width=width)
            snapshot(width, expected_draft="", expected_input_row=1)
        output = "\n".join(pane.cmd("capture-pane", "-p", "-S", "-").stdout)
        for index in range(40):
            assert output.count(f"history marker {index:02}") == 1
    finally:
        server.kill()


def _run(surface: str, root: Path, draft: str, inputbox_max_width: int | None) -> None:
    print("\n".join(f"history marker {i:02}" for i in range(40)), flush=True)

    def attach(ui):
        ui.prompt.replace_input(draft)

        def rendered(app):
            size = app.renderer._last_size
            if size is None:
                return
            path = root / "rendered.tmp"
            path.write_text(
                json.dumps(
                    {
                        "width": size.columns,
                        "height": size.rows,
                        "waiting_for_cpr": app.renderer.waiting_for_cpr,
                        "draft": ui.prompt.buffer.text,
                        "cursor": ui.prompt.buffer.cursor_position,
                    }
                )
            )
            path.replace(root / "rendered.json")

        ui.app.after_render += rendered

    if surface == "chat":
        import runpy

        from toolang.cli.toolang.commands.chat.tui import ChatTuiApp

        original_init = ChatTuiApp.__init__

        def init(self, *args, **kwargs):
            kwargs["inputbox_max_width"] = inputbox_max_width
            original_init(self, *args, **kwargs)
            attach(self)

        sys.argv = ["resize", str(root)]
        with patch.object(ChatTuiApp, "__init__", init):
            runpy.run_module("tests.support.chat_tui_e2e", run_name="__main__")
    else:
        from tests.support.conversations import conversation_record
        from toolang.cli.common.terminal_surfaces import DARK_TERMINAL_SURFACES
        from toolang.cli.toolang.commands.talk.tui import TalkTui

        conversation = conversation_record(
            "gc_00000001", "gc", ("human:bryan", "agent:alice"), name="dev"
        )
        ui = TalkTui(
            AsyncMock(),
            conversation,
            "human:bryan",
            root,
            DARK_TERMINAL_SURFACES,
            read_only=False,
            inputbox_max_width=inputbox_max_width,
        )
        ui.connection = "Connected"
        attach(ui)
        ui.app.run()


if __name__ == "__main__":
    _run(sys.argv[1], Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]) or None)
