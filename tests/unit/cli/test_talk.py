"""Talk CLI literals, drafts, scrollback presentation, and tmux identities."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
import json
import os
import subprocess
import sys
from unittest.mock import AsyncMock

from fakeredis import FakeAsyncValkey, FakeServer
import httpx
import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.color import Color, ColorType

from toolang.cli.toolang import main as cli
from toolang.cli.toolang.commands import talk
from toolang.cli.toolang.commands.talk import directory
from toolang.cli.toolang.commands.talk.tui import TalkTui
from toolang.cli.toolang.commands.talk import tui
from toolang.cli.toolang.commands.talk.rendering import message_block, display_text
from toolang.cli.common.terminal_surfaces import (
    DARK_TERMINAL_SURFACES,
    LIGHT_TERMINAL_SURFACES,
)
from toolang.cli.common.input_history import InputHistoryStore
from toolang.teaming.messaging import MessagingClient
from toolang.teaming.backend import Backend
from toolang.teaming.keys import convo_key
from toolang.teaming.config import BackendConfig
from toolang.teaming.errors import SendUnconfirmed
from toolang.teaming.schemas import Message
from toolang.teaming.schemas import Conversation, HubConnection
from toolang.teaming.api import create_app
from toolang.teaming.client import HubClient


def install_hub(monkeypatch, client, *, human="human:bryan", identity="test"):
    connection = HubConnection("http://hub", human, identity)

    @asynccontextmanager
    async def hub(config):
        app = create_app(client(actor=human))
        async with app.router.lifespan_context(app):
            async with HubClient(config, transport=httpx.ASGITransport(app)) as remote:
                yield remote

    for module in (talk, directory):
        monkeypatch.setattr(module, "HubClient", hub)
        monkeypatch.setattr(module, "settings", lambda root: (connection, human))


def typed(name):
    return f"{'agent' if name in {'alice', 'bob'} else 'human'}:{name}"


@pytest.fixture
def messaging_cli(tmp_path, monkeypatch):
    server = FakeServer(server_type="valkey")
    config = BackendConfig("redis://test")

    def client(_config=config, *, actor="human:bryan"):
        return MessagingClient(
            config,
            actor=actor,
            backend=Backend(
                config, client=FakeAsyncValkey(server=server, decode_responses=True)
            ),
        )

    async def register():
        async with client(actor="agent:alice") as agent, client() as human:
            await agent.register("human:bryan")
            await human.create_conversation("dev")
            await human.resolve("agent:alice")

    asyncio.run(register())
    (tmp_path / "config.toml").write_text(
        '[teaming]\nhuman = "bryan"\n[teaming.backend]\nurl = "redis://test"\n'
    )
    install_hub(monkeypatch, client, identity=config.identity)
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
    result = cli.main(["--root", str(tmp_path), "talk", "alice", *words])
    assert result == 0
    assert "Sent " in capsys.readouterr().out

    async def check():
        async with messaging_cli() as client:
            entries = await client.history(
                (await client.resolve("agent:alice")).conversation
            )
            assert len(entries) == 1
            message = Message.decode(entries[0][1]["data"])
            assert message.body == expected and message.sender == "human:bryan"

    asyncio.run(check())


def test_directory_lists_conversations_and_interactive_requires_tty(
    tmp_path, capsys, messaging_cli
):
    assert cli.main(["--root", str(tmp_path), "talk"]) == 0
    output = capsys.readouterr().out
    assert "gc_00000001" in output and "alice" in output and "gc_00000000" in output
    assert cli.main(["--root", str(tmp_path), "talk", "alice"]) == 1
    assert "TTY" in capsys.readouterr().err
    assert cli.main(["--root", str(tmp_path), "team"]) != 0
    assert cli.main(["--root", str(tmp_path), "alice,", "hello"]) != 0


def test_talk_restores_saved_drafts_and_input_history(
    tmp_path, messaging_cli, monkeypatch
):
    human = "human:bryan"
    identity = sha256(
        f"{tmp_path.resolve()}\0{BackendConfig('redis://test').identity}\0{human}".encode()
    ).hexdigest()[:20]
    state = (
        tmp_path
        / ".runtime"
        / "talk-v2"
        / identity
        / sha256(b"gc_00000001").hexdigest()[:20]
    )
    state.mkdir(parents=True)
    (state / "draft.txt").write_text("unfinished message", encoding="utf-8")
    InputHistoryStore(state / "input.jsonl").append("previous message")
    monkeypatch.setattr(talk.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(talk.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(talk, "resolve_launcher", lambda **kwargs: None)
    restored = []

    async def inspect_ui(ui):
        restored.append(ui.prompt.buffer.text)
        assert ui.prompt.history.get_strings() == ["previous message"]
        assert ui.prompt.placeholder == "write a message"
        ui.prompt.replace_input("continued message")
        assert (state / "draft.txt").read_text() == "continued message"

    monkeypatch.setattr(TalkTui, "run", inspect_ui)
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        assert cli.main(["--root", str(tmp_path), "talk", "gc_00000001"]) == 0
    assert restored == ["unfinished message"]


async def agent_pair(factory, *, messages=False):
    async with factory(actor="agent:alice") as alice, factory(actor="agent:bob") as bob:
        await bob.register("human:bryan")
        group = (await alice.resolve("agent:bob", create=True)).conversation
        if messages:
            await alice.send(group, body="hello")
            await bob.send(group, body="hello")
        return group


def test_human_cannot_send_into_agent_dm(tmp_path, capsys, messaging_cli):
    group = asyncio.run(agent_pair(messaging_cli))
    assert cli.main(["--root", str(tmp_path), "talk", group, "join"]) == 1
    assert "read-only" in capsys.readouterr().err.lower()

    async def check():
        async with messaging_cli() as client:
            assert await client.history(group) == []

    asyncio.run(check())


def test_human_observer_sees_both_agents_left_without_a_composer(
    tmp_path, messaging_cli, monkeypatch
):
    group = asyncio.run(agent_pair(messaging_cli, messages=True))
    monkeypatch.setattr(talk.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(talk.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(talk, "resolve_launcher", lambda **kwargs: None)
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
        await ui.show(await ui.client.history(ui.conversation_id))
        lines = output.getvalue().splitlines()
        assert any(line.startswith("• alice") for line in lines)
        assert any(line.startswith("• bob") for line in lines)
        with set_app(ui.app):
            ui.app.renderer.render(ui.app, ui.app.layout)
            assert not any(
                isinstance(window.content, BufferControl)
                for window in ui.app.layout.visible_windows
            )
            ui.connection = "Connected"
            footer = fragment_list_to_text(ui.status_text())
            assert footer.startswith("  @alice,bob") and footer.endswith("bryan  ")
            assert "from " not in footer and "read-only" not in footer
            assert "Enter send" not in str(ui.status_text())
            await ui.send("accidental send")
            assert len(await ui.client.history(ui.conversation_id)) == 2
            await ui.app.cancel_and_wait_for_background_tasks()

    monkeypatch.setattr(TalkTui, "run", inspect_ui)
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        assert cli.main(["--root", str(tmp_path), "talk", group]) == 0


def test_directory_shows_presence_previews_and_does_not_create_conversations(
    tmp_path, capsys, messaging_cli, monkeypatch
):
    monkeypatch.setenv("COLUMNS", "240")

    async def prepare():
        pair = await agent_pair(messaging_cli)
        async with (
            messaging_cli(actor="agent:alice") as alice,
            messaging_cli() as human,
        ):
            await alice.unregister()
            own = (await human.resolve("agent:alice", create=True)).conversation
            # Inject ordered Stream IDs and a malformed record through the backend fixture.
            raw = human._backend._client
            for group, sid, data in (
                (
                    "gc_00000000",
                    "1000-0",
                    Message.create("human:bryan", "public").encode(),
                ),
                (
                    "gc_00000001",
                    "2000-0",
                    Message.create("agent:bob", "older").encode(),
                ),
                (own, "3000-0", "malformed"),
                (
                    pair,
                    "3000-1",
                    Message.create("agent:bob", "hello\x1b[2J\nworld").encode(),
                ),
            ):
                await raw.xadd(convo_key(group, "messages"), {"data": data}, id=sid)
            return pair, own, {g["conversation"] for g in await human.contacts()}

    pair, own, before = asyncio.run(prepare())
    assert cli.main(["--root", str(tmp_path), "talk"]) == 0
    output = capsys.readouterr().out
    rows = [row.split() for row in output.splitlines() if row.strip()]
    assert [row[0] for row in rows if row[0].startswith(("gc_", "dm_"))] == [
        pair,
        own,
        "gc_00000001",
        "gc_00000000",
    ]
    assert "Team" in output and "Convos" in output and "Name" in output
    assert "○agent:alice · ●agent:bob" in output
    assert "●human:bryan" not in output and "○human:bryan" not in output
    assert "bob: hello world" in output and "\x1b" not in output
    assert "Message unavailable" in output and "too talk <id>" in output

    async def unchanged():
        async with messaging_cli() as client:
            assert {g["conversation"] for g in await client.contacts()} == before

    asyncio.run(unchanged())


@pytest.mark.parametrize(
    "timestamp,expected",
    [
        ("2026-10-08T09:10:00+00:00", "09:10"),
        ("2026-10-07T09:10:00+00:00", "10-07 09:10"),
        ("2025-10-08T09:10:00+00:00", "2025-10-08 09:10"),
    ],
)
def test_directory_message_time_is_compact_without_losing_date(timestamp, expected):
    now = datetime(2026, 10, 8, 10, tzinfo=timezone.utc)
    sid = f"{int(datetime.fromisoformat(timestamp).timestamp() * 1000)}-0"
    assert directory._message_time(sid, now) == expected


def test_failed_send_preserves_draft_and_success_does_not_erase_new_typing(
    tmp_path, monkeypatch
):
    async def scenario():
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            client = AsyncMock()
            ui = TalkTui(
                client,
                Conversation(
                    "gc_00000000", "gc", ("human:bryan", "agent:alice"), name="all"
                ),
                "human:bryan",
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
            assert ui.status == ""
            assert "accepted" not in str(ui.status_text())

    asyncio.run(scenario())


@pytest.mark.parametrize("width", [1, 2, 3, 4, 16, 80])
@pytest.mark.parametrize("sender", ["alice", "bryan"])
def test_narrow_rendering_and_terminal_escape_removal(width, sender):
    message = Message.create(
        typed(sender), "hello\x1b[2J\x1b]52;c;secret\x07 **world**"
    )
    output = StringIO()
    console = Console(file=output, width=width, color_system=None)
    console.print(message_block(message, "human:bryan", width, DARK_TERMINAL_SURFACES))
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
            Message.create("agent:alice", "word " * 20 + "\n\n**Last paragraph**"),
            typed(identity),
            40,
            DARK_TERMINAL_SURFACES,
        )
    )
    lines = output.getvalue().splitlines()
    assert lines[0] == "  " + "┄" * 38
    assert lines[1].rstrip() == "• alice"
    assert lines[2].startswith("  word")
    assert all(line.startswith("  ") for line in lines[2:] if line.strip())
    assert output.getvalue().count("•") == 1
    assert output.getvalue().split().count("word") == 20
    assert any(line.rstrip() == "  Last paragraph" for line in lines)


def test_owner_name_is_above_padded_background_at_top_right():
    output = StringIO()
    console = Console(file=output, width=40, color_system=None)
    block = message_block(
        Message.create("human:bryan", "x" * 28 + "\nshort"),
        "human:bryan",
        40,
        DARK_TERMINAL_SURFACES,
    )
    console.print(block)
    lines = output.getvalue().splitlines()
    assert lines[0] == " " * 33 + "bryan ▮"
    assert lines[1] == " " * 40
    assert lines[2] == " " * 10 + "x" * 28 + "  "
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
    assert accents[0].style.bgcolor is None


@pytest.mark.parametrize("name", ["alice", "bob", "agent七"])
@pytest.mark.parametrize("own_message", [False, True])
@pytest.mark.parametrize("surfaces", [DARK_TERMINAL_SURFACES, LIGHT_TERMINAL_SURFACES])
def test_agent_name_and_marker_share_ansi_color_without_dimming(
    name, own_message, surfaces
):
    sender = f"agent:{name}"
    console = Console(width=60)
    segments = list(
        console.render(
            message_block(
                Message.create(sender, "body text"),
                sender if own_message else "human:bryan",
                60,
                surfaces,
            )
        )
    )
    header = next(segment for segment in segments if name in segment.text)
    marker = next(segment for segment in segments if "•" in segment.text)
    body = next(segment for segment in segments if "body text" in segment.text)
    assert header.style is not None and marker.style is not None
    assert header.style.color is not None
    assert header.style.color.type == ColorType.STANDARD
    assert marker.style.color == header.style.color
    assert header.style.dim is False and marker.style.dim is False
    assert header.style.bold
    assert body.style is None or body.style.color is None


def test_agent_name_colors_survive_process_restart_and_message_order():
    script = """
import json
import sys
from rich.console import Console
from toolang.cli.common.terminal_surfaces import DARK_TERMINAL_SURFACES
from toolang.cli.toolang.commands.talk.rendering import message_block
from toolang.teaming.schemas import Message

console = Console(width=60)
colors = {}
for name in sys.argv[1:]:
    block = message_block(Message.create(f"agent:{name}", "body"), "human:reader",
                          60, DARK_TERMINAL_SURFACES)
    segment = next(s for s in console.render(block) if name in s.text)
    colors[name] = segment.style.color.name
print(json.dumps(colors))
"""
    names = ["alice", "bob", "charlie", "agent七"]
    results = []
    for seed, order in (("1", names), ("2", list(reversed(names)))):
        result = subprocess.run(
            [sys.executable, "-c", script, *order],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        )
        results.append(json.loads(result.stdout))
    assert results[0] == results[1]
    assert len(set(results[0].values())) > 1


@pytest.mark.parametrize("identity", ["alice", "bryan"])
@pytest.mark.parametrize("sender", ["alice", "bob", "bryan", "visitor"])
def test_message_marker_shares_name_row_and_name_aligns_with_body(identity, sender):
    output = StringIO()
    console = Console(file=output, width=40, color_system=None)
    agent = sender in {"alice", "bob"}
    block = message_block(
        Message.create(typed(sender), "first\n\nlast"),
        typed(identity),
        40,
        DARK_TERMINAL_SURFACES,
    )
    console.print(block)
    lines = output.getvalue().splitlines()
    marker = "•" if agent else "▮"
    header_index = 1 if agent else 0
    header = lines[header_index]
    body_lines = lines[header_index + 1 :]
    body = next(line for line in body_lines if "first" in line)
    assert marker in header
    assert all(marker not in line for line in body_lines)
    if sender == identity:
        assert header.endswith(sender + " " + marker)
        assert header.index(sender) + len(sender) == 38
    else:
        assert header.startswith(marker + " " + sender)
        assert header.index(sender) == body.index("first") == 2
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
    monkeypatch.setattr(talk.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(talk.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(talk, "resolve_launcher", lambda **kwargs: None)
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
        for columns in (200, 80, 160):
            monkeypatch.setattr(
                ui.app.output, "get_size", lambda: Size(rows=24, columns=columns)
            )
            output.seek(0)
            output.truncate()
            message = Message.create(typed(sender), "word " * 90)
            await ui.show([("1-0", {"data": message.encode()})])
            lines = output.getvalue().splitlines()
            limit = min(columns, int(configured_width or "120"))
            assert all(len(line) <= limit for line in lines)
            assert output.getvalue().split().count("word") == 90
            header = lines[0 if sender == "bryan" else 1]
            assert header.index(sender) == (
                limit - len(sender) - 2 if sender == "bryan" else 2
            )

    monkeypatch.setattr(TalkTui, "run", render_messages)
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        assert cli.main(["--root", str(tmp_path), "talk", "gc_00000000"]) == 0


def test_talk_identity_separates_root_connection_and_human(tmp_path):
    base = talk.talk_identity(tmp_path, "one", "bryan")
    assert (
        len(
            {
                base,
                talk.talk_identity(tmp_path / "other", "one", "bryan"),
                talk.talk_identity(tmp_path, "two", "bryan"),
                talk.talk_identity(tmp_path, "one", "other"),
            }
        )
        == 4
    )


def test_talk_tmux_session_reuses_canonical_window(
    tmp_path, messaging_cli, monkeypatch
):
    from tests.unit.cli.test_tmux_launcher import FakePane, FakeServer, _launcher
    from toolang.cli.common.tmux import MARK_CONTEXT, MARK_PAD

    server, pane = FakeServer(), FakePane(session_id="$shell")
    monkeypatch.setattr(talk.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(talk.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        talk, "resolve_launcher", lambda *, agent: _launcher(server, pane, agent)
    )
    for _ in range(2):
        assert cli.main(["--root", str(tmp_path), "talk", "gc_00000001"]) == 0
    assert len(server.created) == 1
    session = server.sessions[0]
    assert session.session_name == "talk"
    assert session.options["@toolang_talk"] == "talk"
    assert len(session.windows) == 1
    window = session.windows[0]
    assert window.options[MARK_CONTEXT]
    assert window.window_name == window.options["@toolang_convo"] == "gc_00000001"
    assert window.panes[0].options[MARK_PAD] == "talk"
    window.rename_window("my conversation")
    assert cli.main(["--root", str(tmp_path), "talk", "gc_00000001"]) == 0
    assert len(server.created) == 1 and len(session.windows) == 1
    assert window.window_name == "my conversation"
    assert cli.main(["--root", str(tmp_path), "talk", "gc_00000000"]) == 0
    assert len(server.created) == 1 and len(session.windows) == 2
    assert session.windows[1].options["@toolang_convo"] == "gc_00000000"


def test_talk_reopening_after_hub_port_change_uses_a_fresh_window(
    tmp_path, messaging_cli, monkeypatch
):
    from dataclasses import replace

    from tests.unit.cli.test_tmux_launcher import FakePane, FakeServer, _launcher
    from toolang.cli.common.tmux import MARK_CONTEXT

    server, pane = FakeServer(), FakePane(session_id="$shell")
    connection, human = talk.settings(tmp_path)
    monkeypatch.setattr(talk.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(talk.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        talk, "resolve_launcher", lambda *, agent: _launcher(server, pane, agent)
    )
    monkeypatch.setattr(talk, "settings", lambda _root: (connection, human))
    assert cli.main(["--root", str(tmp_path), "talk", "gc_00000001"]) == 0
    session = server.sessions[0]
    first = session.windows[0]

    connection = replace(connection, endpoint="http://hub:7001")
    for _ in range(2):
        assert cli.main(["--root", str(tmp_path), "talk", "gc_00000001"]) == 0
    assert len(server.created) == 1 and len(session.windows) == 2
    second = session.windows[1]
    assert first.options["@toolang_convo"] == second.options["@toolang_convo"]
    assert first.options[MARK_CONTEXT] != second.options[MARK_CONTEXT]


def test_follow_reconnects_from_last_displayed_id_without_replaying_history(
    tmp_path, monkeypatch
):
    from toolang.teaming.errors import BackendUnavailable

    async def scenario():
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            client = AsyncMock()
            first = (
                "100-9",
                {"data": Message.create("agent:alice", "history").encode()},
            )
            second = (
                "100-10",
                {"data": Message.create("agent:alice", "during disconnect").encode()},
            )
            client.history.return_value = [first]
            client.read.side_effect = [
                BackendUnavailable("offline"),
                [second],
                asyncio.CancelledError(),
            ]
            client.check_cursor.return_value = None
            ui = TalkTui(
                client,
                Conversation(
                    "gc_00000000", "gc", ("human:bryan", "agent:alice"), name="all"
                ),
                "human:bryan",
                tmp_path,
                DARK_TERMINAL_SURFACES,
                read_only=False,
            )
            client.conversation.return_value = ui.conversation
            client.team.return_value = []
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
            client.contacts.assert_not_awaited()
            client.agents.assert_not_awaited()
            assert [call.kwargs["after"] for call in client.read.call_args_list] == [
                "100-9",
                "100-9",
                "100-10",
            ]
            notice.assert_not_awaited()
            assert ui.connection == "Connected"

    asyncio.run(scenario())


@pytest.mark.parametrize("arguments", [["talk"], ["talk", "gc_00000000", "hello"]])
def test_messaging_commands_work_without_config_file(
    tmp_path, monkeypatch, capsys, arguments
):
    server = FakeServer(server_type="valkey")
    expected = BackendConfig("redis://localhost:6379/0")

    def client(config=expected, *, actor):
        assert config == expected
        return MessagingClient(
            config,
            actor=actor,
            backend=Backend(
                config, client=FakeAsyncValkey(server=server, decode_responses=True)
            ),
        )

    install_hub(monkeypatch, client, identity=expected.identity)
    assert not (tmp_path / "config.toml").exists()
    assert cli.main(["--root", str(tmp_path), *arguments]) == 0
    assert "Error" not in capsys.readouterr().err


pytestmark = pytest.mark.usefixtures("fixed_conversation_ids")


@pytest.mark.parametrize("send", [False, True])
def test_opening_empty_dm_writes_only_on_first_send(
    tmp_path, messaging_cli, monkeypatch, send
):
    from tests.unit.messaging.test_protocol import snapshot

    monkeypatch.setattr(talk.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(talk.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(talk, "resolve_launcher", lambda **kwargs: None)

    async def inspect_ui(ui):
        assert ui.selection == "alice" and ui.conversation.kind == "dm"
        assert ui.conversation.revision == 0 and ui.conversation.created_at is None
        async with messaging_cli() as human:
            raw = human._backend._client
            before = await snapshot(raw)
            assert not (await human.resolve("alice")).exists
            assert all(
                row["conversation"] != ui.conversation_id
                for row in await human.contacts()
            )
            ui.save_draft()
            assert await snapshot(raw) == before
            if send:
                await ui.send("first interactive message")
                assert ui.selection is None
                assert (await human.resolve("alice")).exists
                assert len(await human.history(ui.conversation_id)) == 1
                assert (await human.statistics())["dm_count"] == 1
            else:
                assert not (await human.resolve("alice")).exists

    monkeypatch.setattr(TalkTui, "run", inspect_ui)
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        assert cli.main(["--root", str(tmp_path), "talk", "alice"]) == 0


def test_empty_dm_follow_discovers_other_participants_first_send(tmp_path, monkeypatch):
    from toolang.teaming.schemas import Resolution
    from toolang.teaming.ids import dm_id

    async def scenario():
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            client = AsyncMock()
            pair = ("agent:alice", "human:bryan")
            ref = dm_id(*pair)
            client.team.return_value = []
            client.resolve.side_effect = [
                Resolution(ref, pair, False),
                Resolution(ref, pair, True),
            ]
            client.conversation.return_value = Conversation(
                ref, "dm", pair, name="New label", revision=1
            )
            client.history.return_value = [
                (
                    "10-0",
                    {"data": Message.create("agent:alice", "created by peer").encode()},
                )
            ]
            client.read.side_effect = asyncio.CancelledError()
            client.check_cursor.return_value = None
            ui = TalkTui(
                client,
                Conversation(ref, "dm", pair),
                "human:bryan",
                tmp_path,
                DARK_TERMINAL_SURFACES,
                read_only=False,
                selection="alice",
            )
            shown = AsyncMock()
            monkeypatch.setattr(ui, "show", shown)
            # Title publication itself is synchronous.
            titles = []
            monkeypatch.setattr(ui, "write_title", lambda text: titles.append(text))

            async def wait(_):
                return None

            monkeypatch.setattr(tui.asyncio, "sleep", wait)
            with pytest.raises(asyncio.CancelledError):
                await ui.follow()
            assert ui.selection is None and ui.conversation.name == "New label"
            client.send.assert_not_awaited()
            client.history.assert_awaited_once_with(ref)
            shown.assert_awaited_once_with(client.history.return_value)
            assert titles == ["@New label"]

    asyncio.run(scenario())


@pytest.mark.parametrize("selector", ["--dm", "--gc", "--group", "--name"])
def test_selector_flags_are_not_supported(tmp_path, messaging_cli, selector):
    assert cli.main(["--root", str(tmp_path), "talk", selector, "alice"]) != 0
