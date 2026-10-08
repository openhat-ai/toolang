"""Text's bounded input, footer, and scrollback rendering."""

import asyncio
from contextlib import asynccontextmanager
from io import StringIO
from unittest.mock import AsyncMock

from prompt_toolkit.application import create_app_session
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.input import DummyInput
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from toolang.cli.common.terminal_surfaces import LIGHT_TERMINAL_SURFACES
from toolang.cli.toolang.commands.text import tui
from toolang.messaging.schemas import Message


class TerminalOutput(DummyOutput):
    columns = 200
    rows = 30

    def get_size(self):
        return Size(rows=self.rows, columns=self.columns)


@asynccontextmanager
async def text_app(tmp_path):
    output = TerminalOutput()
    with create_app_session(input=DummyInput(), output=output):
        app = tui.TextTui(
            AsyncMock(), "dm_alice", "bryan", tmp_path, LIGHT_TERMINAL_SURFACES
        )
        with set_app(app.app):
            try:
                yield app, output
            finally:
                await app.app.cancel_and_wait_for_background_tasks()


def test_input_and_footer_share_message_width_after_resize_and_clear(tmp_path):
    async def scenario():
        async with text_app(tmp_path) as (ui, output):
            for columns in (200, 60, 180):
                output.columns = columns
                width = min(columns, 120)
                for draft in ("", "hello", "x" * 170, ""):
                    ui.prompt.replace_input(draft)
                    ui.app.render_counter += 1
                    ui.app.renderer.render(ui.app, ui.app.layout)
                    screen = ui.app.renderer.last_rendered_screen
                    assert screen is not None
                    rows = 2 + max(1, (len(draft) + width - 4) // (width - 4))
                    assert ui.prompt.rows() == rows
                    for column in range(columns):
                        cell = screen.data_buffer[0][column]
                        assert cell.char == " "
                        assert not ui.app.style.get_attrs_for_style_str(
                            cell.style
                        ).bgcolor
                    for row in range(1, rows + 1):
                        for column in range(columns):
                            cell = screen.data_buffer[row][column]
                            attrs = ui.app.style.get_attrs_for_style_str(cell.style)
                            if 0 < column < width:
                                assert attrs.bgcolor == "e3e3e3"
                            elif column >= width:
                                assert not attrs.bgcolor
                    footer = "".join(
                        screen.data_buffer[rows + 1][column].char
                        for column in range(columns)
                    ).rstrip()
                    assert len(footer) <= width
                    assert "alice" in footer and "dm_alice" not in footer
                    assert "Ctrl+Q quit" in footer

    asyncio.run(scenario())


def test_input_spacing_preserves_multiline_editing_in_short_terminals(tmp_path):
    async def scenario():
        async with text_app(tmp_path) as (ui, output):
            draft = "one\ntwo\nthree\nfour\nfive\nsix"
            ui.prompt.replace_input(draft)
            for rows in (30, 8, 5, 4, 30):
                output.rows = rows
                ui.app.render_counter += 1
                ui.app.renderer.render(ui.app, ui.app.layout)
                screen = ui.app.renderer.last_rendered_screen
                assert screen is not None
                lines = [
                    "".join(
                        screen.data_buffer[row][column].char for column in range(120)
                    )
                    for row in range(screen.height)
                ]
                assert not any("Window too small" in line for line in lines)
                assert any("six" in line for line in lines)
                assert any("Ctrl+Q quit" in line for line in lines)
                assert ui.prompt.buffer.text == draft
                if rows >= 5:
                    assert not lines[0].strip()
                    assert not ui.app.style.get_attrs_for_style_str(
                        screen.data_buffer[0][1].style
                    ).bgcolor

    asyncio.run(scenario())


def test_history_batch_uses_one_scrollback_write(tmp_path, monkeypatch):
    async def scenario():
        async with text_app(tmp_path) as (ui, _output):
            output = StringIO()
            monkeypatch.setattr(
                tui,
                "terminal_console",
                lambda *, width: Console(file=output, width=width, color_system=None),
            )
            write = AsyncMock(side_effect=lambda action: action())
            monkeypatch.setattr(tui, "run_in_terminal", write)
            ui.agents = {"alice"}
            entries = [
                (f"1-{index}", {"data": Message.create("alice", body).encode()})
                for index, body in enumerate(("first message", "second message"))
            ]
            await ui.show(entries)
            assert write.await_count == 1
            assert ui.cursor == "1-1"
            rendered = output.getvalue()
            assert rendered.index("first message") < rendered.index("second message")
            assert not rendered.startswith("\n")

    asyncio.run(scenario())


def test_footer_expires_sent_status_and_prioritizes_reconnection(tmp_path, monkeypatch):
    async def scenario():
        async with text_app(tmp_path) as (ui, output):
            monkeypatch.setattr(tui, "monotonic", lambda: 100.0)
            ui.prompt.replace_input("hello")
            await ui.send("hello")

            def footer():
                return fragment_list_to_text(ui.status_text())

            assert "alice · Connected · Sent" in footer()
            assert "Enter send" in footer()
            monkeypatch.setattr(tui, "monotonic", lambda: 102.0)
            assert "Sent" not in footer()
            assert "Connected" in footer()
            ui.connection = "Reconnecting…"
            assert "Reconnecting…" in footer() and "Sent" not in footer()
            for width in (1, 12, 29, 30, 60):
                output.columns = width
                assert len(footer()) <= width
                if width >= 30:
                    assert "Ctrl+Q quit" in footer()

    asyncio.run(scenario())
