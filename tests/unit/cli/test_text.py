"""Text CLI literals, drafts, scrollback presentation, and tmux identities."""

import asyncio
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import AsyncMock

from fakeredis import FakeAsyncValkey, FakeServer
import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.color import Color

from toolang.cli.toolang import main as cli
from toolang.cli.toolang.commands import text, team
from toolang.cli.toolang.commands.text.tui import TextTui
from toolang.cli.toolang.commands.text import tui
from toolang.cli.toolang.commands.text.rendering import message_block, display_text
from toolang.cli.common.terminal_surfaces import DARK_TERMINAL_SURFACES
from toolang.messaging.client import MessagingClient, group_key
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
    assert "dm_alice" in output
    assert cli.main(["--root", str(tmp_path), "text", "alice"]) == 1
    assert "TTY" in capsys.readouterr().err
    assert cli.main(["--root", str(tmp_path), "alice,", "hello"]) != 0


def test_human_cannot_send_into_agent_dm(tmp_path, capsys, messaging_cli):
    async def prepare():
        async with messaging_cli() as client:
            await client.register("bob", "bryan", "bob-token")

    asyncio.run(prepare())
    assert cli.main(["--root", str(tmp_path), "text", "dm_alice_bob", "join"]) == 1
    assert "read-only" in capsys.readouterr().err.lower()

    async def check():
        async with messaging_cli() as client:
            assert await client.history("dm_alice_bob") == []

    asyncio.run(check())


def test_human_observer_sees_both_agents_left_without_a_composer(
    tmp_path, messaging_cli, monkeypatch
):
    async def prepare():
        async with messaging_cli() as client:
            await client.register("bob", "bryan", "bob-token")
            for sender in ("alice", "bob"):
                await client.send(
                    "dm_alice_bob", sender=sender, agent=True, body="hello"
                )
            await client.redis.xadd(
                group_key("dm_alice_bob", "msg"),
                {"data": Message.create("bryan", "old human message").encode()},
            )

    asyncio.run(prepare())
    monkeypatch.setattr(text.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(text.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(text, "resolve_launcher", lambda **kwargs: None)
    monkeypatch.setenv("TOOLANG_COLOR_SCHEME", "dark")
    output = StringIO()
    monkeypatch.setattr(
        tui,
        "terminal_console",
        lambda *, width: Console(file=output, width=width, color_system=None),
    )

    async def write_now(write):
        write()

    monkeypatch.setattr(tui, "run_in_terminal", write_now)

    async def inspect_ui(ui):
        ui.agents = {"alice", "bob"}
        await ui.show(await ui.client.history(ui.group))
        lines = output.getvalue().splitlines()
        assert next(line for line in lines if line.strip() == "alice").startswith(
            "alice"
        )
        assert next(line for line in lines if line.strip() == "bob").startswith("bob")
        assert "old human message" in output.getvalue()
        with set_app(ui.app):
            ui.app.renderer.render(ui.app, ui.app.layout)
            assert not any(
                isinstance(control, BufferControl)
                for control in ui.app.layout.find_all_controls()
            )
            assert "@alice ↔ @bob · Read-only" in str(ui.status_text())
            assert "Enter send" not in str(ui.status_text())
            await ui.send("accidental send")
            assert len(await ui.client.history(ui.group)) == 3
            await ui.app.cancel_and_wait_for_background_tasks()

    monkeypatch.setattr(TextTui, "run", inspect_ui)
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        assert cli.main(["--root", str(tmp_path), "text", "dm_alice_bob"]) == 0


def test_team_directory_shows_existing_dms_presence_and_recent_previews(
    tmp_path, capsys, messaging_cli, monkeypatch
):
    monkeypatch.setenv("COLUMNS", "180")

    async def prepare():
        async with messaging_cli() as client:
            await client.register("bob", "bryan", "bob-token")
            await client.register("carol", "bryan", "carol-token")
            await client.unregister("alice", "token")
            await client.resolve("dm_alice_bob")
            for group, sid, data in (
                ("all", "1000-0", Message.create("bryan", "public").encode()),
                ("gc_dev", "2000-0", Message.create("bob", "older").encode()),
                ("dm_alice", "3000-0", "malformed"),
                (
                    "dm_alice_bob",
                    "3000-1",
                    Message.create("bob", "hello\x1b[2J\nworld").encode(),
                ),
            ):
                await client.redis.xadd(group_key(group, "msg"), {"data": data}, id=sid)
            return await client.group_ids()

    before = asyncio.run(prepare())
    assert cli.main(["--root", str(tmp_path), "team"]) == 0
    output = capsys.readouterr().out
    rows = [row.split() for row in output.splitlines() if row.strip()]
    assert [row[0] for row in rows[1:-1]] == [
        "all",
        "dm_alice_bob",
        "dm_alice",
        "gc_dev",
        "dm_bob",
        "dm_carol",
    ]
    assert "○alice ↔ ●bob" in output
    assert "bryan ↔ ○alice" in output
    assert "●bryan" not in output and "○bryan" not in output
    assert "bob: hello world" in output and "\x1b" not in output
    assert "Message unavailable" in output
    assert "too text <target>" in output

    async def unchanged():
        async with messaging_cli() as client:
            assert await client.group_ids() == before
            assert "dm_bob_carol" not in before

    asyncio.run(unchanged())


@pytest.mark.parametrize(
    "timestamp,expected",
    [
        ("2026-10-08T09:10:00+00:00", "09:10"),
        ("2026-10-07T09:10:00+00:00", "10-07 09:10"),
        ("2025-10-08T09:10:00+00:00", "2025-10-08 09:10"),
    ],
)
def test_team_message_time_is_compact_without_losing_date(timestamp, expected):
    now = datetime(2026, 10, 8, 10, tzinfo=timezone.utc)
    sid = f"{int(datetime.fromisoformat(timestamp).timestamp() * 1000)}-0"
    assert team._message_time(sid, now) == expected


def test_failed_send_preserves_draft_and_success_does_not_erase_new_typing(
    tmp_path, monkeypatch
):
    async def scenario():
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            client = AsyncMock()
            ui = TextTui(
                client,
                "all",
                "bryan",
                tmp_path,
                DARK_TERMINAL_SURFACES,
                read_only=False,
            )
            notice = AsyncMock()
            monkeypatch.setattr(ui, "print_notice", notice)
            ui.prompt.replace_input("original")
            client.send.side_effect = SendUnconfirmed(
                "Send not confirmed (message recoverable-id)"
            )
            await ui.send("original")
            assert ui.prompt.buffer.text == "original"
            assert ui.draft.read_text() == "original"
            notice.assert_awaited_once_with(
                "Send not confirmed (message recoverable-id)"
            )
            client.send.side_effect = None
            client.send.return_value = {"message": {"id": "accepted"}}
            ui.prompt.replace_input("new draft")
            await ui.send("original")
            assert ui.prompt.buffer.text == "new draft"
            assert ui.prompt.history.get_strings() == ["original"]
            await ui.send("new draft")
            assert ui.prompt.buffer.text == ""
            assert ui.status == "Sent"
            assert "accepted" not in str(ui.status_text())

    asyncio.run(scenario())


@pytest.mark.parametrize("width", [1, 2, 3, 4, 16, 80])
@pytest.mark.parametrize("sender", ["alice", "bryan"])
def test_narrow_rendering_and_terminal_escape_removal(width, sender):
    message = Message.create(sender, "hello\x1b[2J\x1b]52;c;secret\x07 **world**")
    output = StringIO()
    console = Console(file=output, width=width, color_system=None)
    console.print(
        message_block(message, "bryan", {"alice"}, width, DARK_TERMINAL_SURFACES)
    )
    rendered = output.getvalue()
    assert "secret" not in rendered and "\x1b" not in rendered
    assert all(len(line) <= width for line in rendered.splitlines())
    assert "hello" in rendered.replace(" ", "").replace("\n", "").replace("▮", "")
    assert display_text("a\x08b\r\x00c") == "abc"


@pytest.mark.parametrize("identity", ["bryan", "bob"])
def test_left_message_marker_has_aligned_header_and_wrapped_body(identity):
    output = StringIO()
    console = Console(file=output, width=40, color_system=None)
    console.print(
        message_block(
            Message.create("alice", "word " * 20 + "\n\n**Last paragraph**"),
            identity,
            {"alice", "bob"},
            40,
            DARK_TERMINAL_SURFACES,
        )
    )
    lines = output.getvalue().splitlines()
    assert lines[0].rstrip() == "alice"
    assert lines[1].startswith("• word")
    assert all(line.startswith("  ") for line in lines[2:] if line.strip())
    assert output.getvalue().count("•") == 1
    assert output.getvalue().split().count("word") == 20
    assert any(line.rstrip() == "  Last paragraph" for line in lines)


def test_owner_name_is_above_padded_background_at_top_right():
    output = StringIO()
    console = Console(file=output, width=40, color_system=None)
    block = message_block(
        Message.create("bryan", "x" * 28 + "\nshort"),
        "bryan",
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


@pytest.mark.parametrize("identity", ["alice", "bryan"])
@pytest.mark.parametrize("sender", ["alice", "bob", "bryan", "visitor"])
def test_message_side_follows_identity_and_name_aligns_with_marker(identity, sender):
    output = StringIO()
    console = Console(file=output, width=40, color_system=None)
    agent = sender in {"alice", "bob"}
    block = message_block(
        Message.create(sender, "first\n\nlast"),
        identity,
        {"alice", "bob"},
        40,
        DARK_TERMINAL_SURFACES,
    )
    console.print(block)
    lines = output.getvalue().splitlines()
    marker = "•" if agent else "▮"
    body = next(line for line in lines if marker in line)
    if sender == identity:
        assert lines[0].endswith(sender)
        assert body.index(marker) == 39
    else:
        assert lines[0].startswith(sender)
        assert body.startswith(marker + " first")
    assert output.getvalue().count(marker) == 1
    background = any(
        segment.style and segment.style.bgcolor for segment in console.render(block)
    )
    assert background == (not agent)


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
                limit - len(sender) if sender == "bryan" else 0
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
            ui = TextTui(
                client,
                "all",
                "bryan",
                tmp_path,
                DARK_TERMINAL_SURFACES,
                read_only=False,
            )
            shown = []

            async def show(entries):
                shown.extend(sid for sid, _ in entries)
                ui.cursor = entries[-1][0]

            monkeypatch.setattr(ui, "show", show)
            notice = AsyncMock()
            monkeypatch.setattr(ui, "print_notice", notice)
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
            notice.assert_not_awaited()
            assert ui.connection == "Connected"

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
