"""Text CLI literals, drafts, scrollback presentation, and tmux identities."""

import asyncio
from io import StringIO
from unittest.mock import AsyncMock

from fakeredis import FakeAsyncValkey, FakeServer
import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.color import Color

from toolang.cli.toolang import main as cli
from toolang.cli.toolang.commands import text, team
from toolang.cli.toolang.commands.text.tui import TextTui
from toolang.cli.toolang.commands.text import tui
from toolang.cli.toolang.commands.text.rendering import message_block, display_text
from toolang.cli.common.terminal_surfaces import DARK_TERMINAL_SURFACES
from toolang.messaging.client import MessagingClient
from toolang.messaging.config import MessagingConfig
from toolang.messaging.errors import SendUnconfirmed
from toolang.messaging.schemas import Message


@pytest.fixture
def messaging_cli(tmp_path, monkeypatch):
    server = FakeServer(server_type="valkey")
    config = MessagingConfig("redis://test", ("gc_dev",))

    def client(_config=config):
        return MessagingClient(
            config, client=FakeAsyncValkey(server=server, decode_responses=True)
        )

    async def register():
        async with client() as connected:
            await connected.register("alice", "bryan", "token")

    asyncio.run(register())
    (tmp_path / "config.toml").write_text(
        '[messaging]\nurl = "redis://test"\n[human]\nname = "bryan"\n'
    )
    monkeypatch.setattr(text, "MessagingClient", client)
    monkeypatch.setattr(team, "MessagingClient", client)
    return client


@pytest.mark.parametrize(
    "words,expected",
    [
        (["hello", "world"], "hello world"),
        (["--", "hi", "-sdf", "--help"], "hi -sdf --help"),
        (["--root", "elsewhere", "--dm", "--help"], "--root elsewhere --dm --help"),
        ([" line one\nline two  "], " line one\nline two  "),
        (["--", "--", "data"], "-- data"),
    ],
)
def test_send_body_is_literal_and_exits_on_ack(
    tmp_path, capsys, messaging_cli, words, expected
):
    result = cli.main(["--root", str(tmp_path), "text", "alice", *words])
    assert result == 0
    assert "Sent " in capsys.readouterr().out

    async def check():
        async with messaging_cli() as client:
            entries = await client.history("dm_alice")
            assert len(entries) == 1
            message = Message.decode(entries[0][1]["data"])
            assert message.body == expected and message.sender == "bryan"

    asyncio.run(check())


def test_team_lists_groups_and_interactive_requires_tty(
    tmp_path, capsys, messaging_cli
):
    assert cli.main(["--root", str(tmp_path), "team"]) == 0
    output = capsys.readouterr().out
    assert "gc_dev" in output and "alice" in output and "all" in output
    assert "dm_alice" not in output
    assert cli.main(["--root", str(tmp_path), "text", "alice"]) == 1
    assert "TTY" in capsys.readouterr().err
    assert cli.main(["--root", str(tmp_path), "alice,", "hello"]) != 0


def test_failed_send_preserves_draft_and_success_does_not_erase_new_typing(
    tmp_path, monkeypatch
):
    async def scenario():
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            client = AsyncMock()
            ui = TextTui(client, "all", "bryan", tmp_path, DARK_TERMINAL_SURFACES)
            monkeypatch.setattr(ui, "print_notice", AsyncMock())
            ui.prompt.replace_input("original")
            client.send.side_effect = SendUnconfirmed("Send not confirmed")
            await ui.send("original")
            assert ui.prompt.buffer.text == "original"
            assert ui.draft.read_text() == "original"
            client.send.side_effect = None
            client.send.return_value = {"message": {"id": "accepted"}}
            ui.prompt.replace_input("new draft")
            await ui.send("original")
            assert ui.prompt.buffer.text == "new draft"
            assert ui.prompt.history.get_strings() == ["original"]
            await ui.send("new draft")
            assert ui.prompt.buffer.text == ""
            assert "accepted" in ui.status

    asyncio.run(scenario())


@pytest.mark.parametrize("width", [1, 2, 3, 4, 16, 80])
@pytest.mark.parametrize("sender", ["alice", "bryan"])
def test_narrow_rendering_and_terminal_escape_removal(width, sender):
    message = Message.create(sender, "hello\x1b[2J\x1b]52;c;secret\x07 **world**")
    output = StringIO()
    console = Console(file=output, width=width, color_system=None)
    console.print(
        message_block(message, "all", {"alice"}, width, DARK_TERMINAL_SURFACES)
    )
    rendered = output.getvalue()
    assert "secret" not in rendered and "\x1b" not in rendered
    assert all(len(line) <= width for line in rendered.splitlines())
    assert "hello" in rendered.replace(" ", "").replace("\n", "").replace("▮", "")
    assert display_text("a\x08b\r\x00c") == "abc"


@pytest.mark.parametrize("group", ["all", "dm_alice", "dm_alice_bob"])
def test_left_message_marker_has_aligned_header_and_wrapped_body(group):
    output = StringIO()
    console = Console(file=output, width=40, color_system=None)
    console.print(
        message_block(
            Message.create("alice", "word " * 20 + "\n\n**Last paragraph**"),
            group,
            {"alice", "bob"},
            40,
            DARK_TERMINAL_SURFACES,
        )
    )
    lines = output.getvalue().splitlines()
    assert lines[0].rstrip() == "  alice"
    assert lines[1].startswith("• word")
    assert all(line.startswith("  ") for line in lines[2:] if line.strip())
    assert output.getvalue().count("•") == 1
    assert output.getvalue().split().count("word") == 20
    assert any(line.rstrip() == "  Last paragraph" for line in lines)


@pytest.mark.parametrize("group", ["all", "dm_alice"])
def test_owner_name_is_above_padded_background_at_top_right(group):
    output = StringIO()
    console = Console(file=output, width=40, color_system=None)
    block = message_block(
        Message.create("bryan", "x" * 28 + "\nshort"),
        group,
        {"alice"},
        40,
        DARK_TERMINAL_SURFACES,
    )
    console.print(block)
    lines = output.getvalue().splitlines()
    assert lines[0] == " " * 35 + "bryan"
    assert lines[1] == " " * 40
    assert lines[2] == " " * 10 + "x" * 28 + " ▮"
    assert lines[3] == " " * 10 + "short" + " " * 25
    assert lines[4:] == [" " * 40, " " * 40]
    background_widths = [
        sum(
            segment.cell_length
            for segment in row
            if segment.style and segment.style.bgcolor is not None
        )
        for row in console.render_lines(block)
    ]
    assert background_widths == [0, 32, 32, 32, 32, 0]
    accents = [segment for segment in console.render(block) if "▮" in segment.text]
    assert len(accents) == 1
    assert accents[0].style is not None
    assert accents[0].style.color == Color.parse("bright_cyan")
    assert accents[0].style.dim is False


@pytest.mark.parametrize("configured_width", [None, "72"])
@pytest.mark.parametrize("sender", ["bryan", "alice"])
def test_interactive_messages_use_chat_width_after_resize(
    tmp_path, monkeypatch, messaging_cli, configured_width, sender
):
    if configured_width is None:
        monkeypatch.delenv("TOOLANG_PROGRESS_MAX_WIDTH", raising=False)
    else:
        monkeypatch.setenv("TOOLANG_PROGRESS_MAX_WIDTH", configured_width)
    monkeypatch.setenv("TOOLANG_COLOR_SCHEME", "dark")
    monkeypatch.setattr(text.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(text.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(text, "resolve_launcher", lambda **kwargs: None)
    output = StringIO()
    monkeypatch.setattr(
        tui,
        "terminal_console",
        lambda *, width: Console(file=output, width=width, color_system=None),
    )

    async def write_now(write):
        write()

    monkeypatch.setattr(tui, "run_in_terminal", write_now)

    async def render_messages(ui):
        ui.agents = {"alice"}
        for columns in (200, 80, 160):
            monkeypatch.setattr(
                ui.app.output, "get_size", lambda: Size(rows=24, columns=columns)
            )
            output.seek(0)
            output.truncate()
            message = Message.create(sender, "word " * 90)
            await ui.show([("1-0", {"data": message.encode()})])
            lines = output.getvalue().splitlines()
            limit = min(columns, int(configured_width or "120"))
            assert all(len(line) <= limit for line in lines)
            assert output.getvalue().split().count("word") == 90
            assert lines[0].index(sender) == (
                limit - len(sender) if sender == "bryan" else 2
            )

    monkeypatch.setattr(TextTui, "run", render_messages)
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        assert cli.main(["--root", str(tmp_path), "text", "all"]) == 0


def test_text_tmux_identity_separates_root_connection_and_human(tmp_path):
    base = text.text_identity(tmp_path, "one", "bryan")
    assert (
        len(
            {
                base,
                text.text_identity(tmp_path / "other", "one", "bryan"),
                text.text_identity(tmp_path, "two", "bryan"),
                text.text_identity(tmp_path, "one", "other"),
            }
        )
        == 4
    )


def test_follow_reconnects_from_last_displayed_id_without_replaying_history(
    tmp_path, monkeypatch
):
    from valkey.exceptions import ConnectionError

    async def scenario():
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            client = AsyncMock()
            client.agents.return_value = {"alice": "bryan"}
            first = ("100-9", {"data": Message.create("alice", "history").encode()})
            second = (
                "100-10",
                {"data": Message.create("alice", "during disconnect").encode()},
            )
            client.history.return_value = [first]
            client.read.side_effect = [
                ConnectionError("offline"),
                [second],
                asyncio.CancelledError(),
            ]
            client.check_cursor.return_value = None
            ui = TextTui(client, "all", "bryan", tmp_path, DARK_TERMINAL_SURFACES)
            shown = []

            async def show(entries):
                shown.extend(sid for sid, _ in entries)
                ui.cursor = entries[-1][0]

            monkeypatch.setattr(ui, "show", show)
            monkeypatch.setattr(ui, "print_notice", AsyncMock())
            sleep = asyncio.sleep

            async def yield_once(_seconds):
                await sleep(0)

            monkeypatch.setattr(asyncio, "sleep", yield_once)
            with pytest.raises(asyncio.CancelledError):
                await ui.follow()
            assert shown == ["100-9", "100-10"]
            client.history.assert_awaited_once()
            assert [call.kwargs["after"] for call in client.read.call_args_list] == [
                "100-9",
                "100-9",
                "100-10",
            ]

    asyncio.run(scenario())


@pytest.mark.parametrize("arguments", [["team"], ["text", "all", "hello"]])
def test_messaging_commands_work_without_config_file(
    tmp_path, monkeypatch, capsys, arguments
):
    server = FakeServer(server_type="valkey")
    expected = MessagingConfig("redis://localhost:6379/0")

    def client(config):
        assert config == expected
        return MessagingClient(
            config, client=FakeAsyncValkey(server=server, decode_responses=True)
        )

    async def register():
        async with client(expected) as connection:
            await connection.register("alice", "owner", "token")

    asyncio.run(register())
    monkeypatch.setattr(team, "MessagingClient", client)
    monkeypatch.setattr(text, "MessagingClient", client)
    assert not (tmp_path / "config.toml").exists()
    assert cli.main(["--root", str(tmp_path), *arguments]) == 0
    assert "Error" not in capsys.readouterr().err
