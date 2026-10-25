"""Talk's bounded input, footer, and scrollback rendering."""

import asyncio

from tests.support.conversations import conversation_record
from contextlib import asynccontextmanager
from io import StringIO
from unittest.mock import AsyncMock

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.input import DummyInput
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from toolang.cli.common.terminal_surfaces import LIGHT_TERMINAL_SURFACES
from toolang.cli.toolang.commands.talk import tui
from toolang.teaming.schemas import Message
from toolang.teaming.client import HubClient


class TerminalOutput(DummyOutput):
    columns = 200
    rows = 30

    def get_size(self):
        return Size(rows=self.rows, columns=self.columns)


@asynccontextmanager
async def talk_app(
    tmp_path,
    *,
    conversation=None,
    human="human:bryan",
    read_only=False,
    max_width=120,
    inputbox_max_width=None,
):
    output = TerminalOutput()
    conversation = conversation or conversation_record(
        "gc_00000001", "gc", (human, "agent:alice", "agent:bob"), name="dev"
    )
    client = AsyncMock(spec=HubClient)
    client.conversation.return_value = conversation
    client.team.return_value = []
    with create_app_session(input=DummyInput(), output=output):
        app = tui.TalkTui(
            client,
            conversation,
            human,
            tmp_path / "input.jsonl",
            LIGHT_TERMINAL_SURFACES,
            read_only=read_only,
            max_width=max_width,
            inputbox_max_width=inputbox_max_width,
        )
        app.connection = "Connected"
        with set_app(app.app):
            try:
                yield app, output
            finally:
                await app.app.cancel_and_wait_for_background_tasks()


@pytest.mark.parametrize("max_width", [72, 120])
@pytest.mark.parametrize("inputbox_max_width", [None, 48, 160])
def test_input_and_footer_share_input_width_after_resize_and_clear(
    tmp_path, max_width, inputbox_max_width
):
    async def scenario():
        async with talk_app(
            tmp_path, max_width=max_width, inputbox_max_width=inputbox_max_width
        ) as (ui, output):
            for columns in (200, 60, 180):
                output.columns = columns
                width = min(columns, inputbox_max_width or max_width)
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
                    assert footer.startswith("  #dev(?/3)")
                    assert footer.endswith("bryan")
                    assert len(footer) == width - 2

    asyncio.run(scenario())


def test_input_spacing_preserves_multiline_editing_in_short_terminals(tmp_path):
    async def scenario():
        async with talk_app(tmp_path) as (ui, output):
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
                assert any("bryan" in line for line in lines)
                assert ui.prompt.buffer.text == draft
                if rows >= 5:
                    assert not lines[0].strip()
                    assert not ui.app.style.get_attrs_for_style_str(
                        screen.data_buffer[0][1].style
                    ).bgcolor

    asyncio.run(scenario())


@pytest.mark.parametrize("rows", [4, 5, 30])
def test_clear_places_input_at_top_until_new_output(tmp_path, monkeypatch, rows):
    async def scenario():
        async with talk_app(tmp_path) as (ui, output):
            output.rows = rows
            ui.prompt.replace_input("draft")
            binding = ui.app.key_bindings.get_bindings_for_keys((Keys.ControlL,))[0]
            for columns in (120, 40, 160):
                output.columns = columns
                binding.handler(None)
                ui.app.render_counter += 1
                ui.app.renderer.render(ui.app, ui.app.layout)
                screen = ui.app.renderer.last_rendered_screen
                assert screen is not None
                assert (
                    ui.app.style.get_attrs_for_style_str(
                        screen.data_buffer[0][1].style
                    ).bgcolor
                    == "e3e3e3"
                )
                assert ui.prompt.buffer.text == "draft"
                assert ui._input_gap_rows() == 0
            monkeypatch.setattr(
                tui, "run_in_terminal", AsyncMock(side_effect=lambda action: action())
            )
            monkeypatch.setattr(
                tui,
                "terminal_console",
                lambda *, width: Console(file=StringIO(), width=width),
            )
            await ui.show(
                [("1-0", {"data": Message.create("agent:alice", "reply").encode()})]
            )
            assert ui._input_gap_rows() == int(rows >= 5)
            binding.handler(None)
            await ui.print_notice("notice")
            assert ui._input_gap_rows() == int(rows >= 5)

    asyncio.run(scenario())


def test_history_batch_uses_one_scrollback_write(tmp_path, monkeypatch):
    async def scenario():
        async with talk_app(tmp_path) as (ui, _output):
            output = StringIO()
            monkeypatch.setattr(
                tui,
                "terminal_console",
                lambda *, width: Console(file=output, width=width, color_system=None),
            )
            write = AsyncMock(side_effect=lambda action: action())
            monkeypatch.setattr(tui, "run_in_terminal", write)
            entries = [
                (f"1-{index}", {"data": Message.create("agent:alice", body).encode()})
                for index, body in enumerate(("first message", "second message"))
            ]
            await ui.show(entries)
            assert write.await_count == 1
            assert ui.cursor == "1-1"
            rendered = output.getvalue()
            assert rendered.index("first message") < rendered.index("second message")
            assert not rendered.startswith("\n")

    asyncio.run(scenario())


def test_footer_keeps_identity_after_send_and_prioritizes_reconnection(tmp_path):
    async def scenario():
        async with talk_app(tmp_path) as (ui, output):
            ui.prompt.replace_input("hello")
            await ui.send("hello")

            def footer():
                return fragment_list_to_text(ui.status_text())

            assert footer().endswith("bryan  ")
            assert "from " not in footer()
            assert "Connected" not in footer() and "Sent" not in footer()
            assert "Enter" not in footer() and "Ctrl" not in footer()
            ui.connection = "Reconnecting…"
            assert footer().rstrip() == "! Reconnecting…"
            assert "bryan" not in footer() and "Sent" not in footer()
            assert "#dev" not in footer()
            for width in (1, 12, 29, 30, 60):
                output.columns = width
                assert len(footer()) <= width

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "conversation,read_only,label",
    [
        (
            conversation_record("gc_00000000", "gc", ("human:bryan",), name="all"),
            False,
            "#all(1/1)",
        ),
        (
            conversation_record("gc_00000003", "gc", ("human:bryan",), name="design"),
            False,
            "#design(1/1)",
        ),
        (
            conversation_record("dm_00000001", "dm", ("agent:alice", "human:bryan")),
            False,
            "@alice",
        ),
        (
            conversation_record("dm_00000002", "dm", ("agent:bob", "agent:alice")),
            True,
            "@alice,bob",
        ),
    ],
)
def test_footer_identifies_conversation_kind(tmp_path, conversation, read_only, label):
    async def scenario():
        async with talk_app(
            tmp_path, conversation=conversation, read_only=read_only
        ) as (ui, _):
            footer = fragment_list_to_text(ui.status_text())
            assert footer.startswith("  " + label)
            assert footer.endswith("bryan  ")
            assert "from " not in footer and "read-only" not in footer
            assert "Ctrl" not in footer

    asyncio.run(scenario())


def test_talk_does_not_read_or_write_unsent_draft(tmp_path):
    old_draft = tmp_path / "draft.txt"
    old_draft.write_text("unsent message")

    async def scenario():
        async with talk_app(tmp_path, read_only=True) as (ui, _):
            assert not ui.prompt.buffer.text
            await ui.send("accidental send")
            ui.prompt.replace_input("typed but unsent")
            assert old_draft.read_text() == "unsent message"
            assert not ui.prompt.history_store.path.exists()

    asyncio.run(scenario())


def test_live_membership_changes_update_input_focus_without_persisting_drafts(
    tmp_path, monkeypatch
):
    human = "human:bryan"
    observer = conversation_record("gc_00000001", "gc", ("agent:alice",), name="dev")
    joined = conversation_record(
        "gc_00000001", "gc", (human, "agent:alice"), name="dev"
    )
    old_draft = tmp_path / "draft.txt"
    old_draft.write_text("saved before joining")

    async def scenario():
        async with talk_app(tmp_path, conversation=observer, read_only=True) as (ui, _):
            ui.client.conversation.return_value = joined
            ui.client.history.return_value = []
            ui.client.read.return_value = []
            ui.client.check_cursor.return_value = None
            polls = 0

            async def after_poll(_delay):
                nonlocal polls
                polls += 1
                if polls == 1:
                    assert not ui.read_only
                    assert ui.app.layout.has_focus(ui.prompt.buffer)
                    assert not ui.prompt.buffer.text
                    ui.prompt.replace_input("unfinished while participating")
                    ui.client.conversation.return_value = observer
                elif polls == 2:
                    assert ui.read_only
                    assert not ui.app.layout.has_focus(ui.prompt.buffer)
                    await ui.send("must not send after leaving")
                    ui.client.send.assert_not_awaited()
                    assert old_draft.read_text() == "saved before joining"
                    assert not ui.prompt.history_store.path.exists()
                    ui.client.conversation.return_value = joined
                else:
                    assert not ui.read_only
                    assert ui.app.layout.has_focus(ui.prompt.buffer)
                    assert ui.prompt.buffer.text == "unfinished while participating"
                    raise asyncio.CancelledError

            monkeypatch.setattr(asyncio, "sleep", after_poll)
            with pytest.raises(asyncio.CancelledError):
                await ui.follow()
            assert polls == 3

    asyncio.run(scenario())


def test_member_notification_wakes_talk_without_polling_or_reloading_history(
    tmp_path, monkeypatch
):
    from dataclasses import asdict
    from contextlib import suppress

    import httpx

    from tests.support.sse import Events
    from toolang.teaming.notifications import Changes
    from toolang.teaming.schemas import HubConnection

    async def scenario():
        stream = Events("checkpoint")
        paths = []
        wait = Changes.wait

        async def without_timer(self, timeout=None):
            await wait(self)

        monkeypatch.setattr(Changes, "wait", without_timer)
        async with talk_app(tmp_path) as (ui, _):
            info = ui.conversation
            loaded = asyncio.Event()
            updated = asyncio.Event()

            async def request(request):
                path = request.url.path
                paths.append(path)
                if path == "/team/events":
                    return httpx.Response(
                        200,
                        headers={"content-type": "text/event-stream"},
                        stream=stream,
                    )
                if path.endswith("/messages"):
                    loaded.set()
                    return httpx.Response(200, json=[])
                if path.endswith("/cursor"):
                    return httpx.Response(200, json={"notice": None})
                return httpx.Response(200, json=asdict(info))

            original = ui.update_membership

            def membership(value):
                original(value)
                updated.set()

            monkeypatch.setattr(ui, "update_membership", membership)
            ui.prompt.replace_input("keep my draft")
            async with HubClient(
                HubConnection("http://hub", "backend"),
                actor=ui.human,
                transport=httpx.MockTransport(request),
            ) as client:
                ui.client = client
                async with client.team_changes(info.id) as changes:
                    following = asyncio.create_task(ui.follow(changes))
                    try:
                        await asyncio.wait_for(loaded.wait(), 2)
                        info = conversation_record(
                            info.id, "gc", ("agent:alice",), name="dev"
                        )
                        await stream.send(
                            "change",
                            {
                                "type": "conversation.member_removed",
                                "conversation": info.id,
                            },
                        )
                        await asyncio.wait_for(updated.wait(), 2)
                        assert ui.read_only
                        assert not ui.prompt.history_store.path.exists()
                        assert paths.count("/team/events") == 1
                        assert "/events/stream" not in paths and "/team" not in paths
                    finally:
                        following.cancel()
                        with suppress(asyncio.CancelledError):
                            await following
        assert stream.closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind,members",
    [
        ("dm", ("agent:alice", "human:bryan")),
        ("gc", ("human:bryan", "human:other")),
        ("dm", ("agent:alice", "agent:bob")),
    ],
)
@pytest.mark.parametrize("failure", [False, True])
def test_startup_reads_one_team_snapshot_before_history(
    tmp_path, monkeypatch, kind, members, failure
):
    from toolang.teaming.errors import BackendUnavailable
    from toolang.teaming.notifications import Changes

    async def scenario():
        info = conversation_record(
            "dm_00000001" if kind == "dm" else "gc_00000001", kind, members
        )
        async with talk_app(tmp_path, conversation=info) as (ui, _):
            ui.prompt.replace_input("keep draft")
            colors = [{"member": "human:bryan", "color": "magenta", "deadline": None}]
            ui.client.team.side_effect = (
                BackendUnavailable("offline") if failure else None
            )
            ui.client.team.return_value = colors
            changes = AsyncMock(spec=Changes)

            async def wait(timeout=None):
                if timeout is not None:
                    await asyncio.Event().wait()

            changes.wait.side_effect = wait

            @asynccontextmanager
            async def subscribe(*args, **kwargs):
                yield changes

            monkeypatch.setattr(ui.client, "team_changes", subscribe)
            loaded = asyncio.Event()

            async def history(*args):
                ui.client.team.assert_awaited_once()
                assert ui.colors == ({} if failure else {"human:bryan": "magenta"})
                loaded.set()
                return []

            ui.client.history.side_effect = history

            async def run(*, pre_run):
                pre_run()
                await asyncio.wait_for(loaded.wait(), 1)
                await ui.app.cancel_and_wait_for_background_tasks()

            monkeypatch.setattr(ui.app, "run_async", run)
            await ui.run()
            assert ui.prompt.buffer.text == "keep draft"

    asyncio.run(scenario())


def test_team_refresh_keeps_cached_colors_on_failure_and_renders_edits(
    tmp_path, monkeypatch
):
    from toolang.teaming.errors import BackendUnavailable

    async def scenario():
        async with talk_app(tmp_path) as (ui, _):
            rows = [{"member": "agent:alice", "color": "green", "deadline": 1}]
            ui.client.team.side_effect = [
                rows,
                BackendUnavailable("offline"),
                [{**rows[0], "color": "#7aa2f7"}],
            ]
            rendered = []
            render = tui.message_block

            def block(*args, color=None):
                rendered.append(color)
                return render(*args, color=color)

            monkeypatch.setattr(tui, "message_block", block)
            output = StringIO()
            monkeypatch.setattr(
                tui,
                "terminal_console",
                lambda *, width: Console(file=output, width=width, color_system=None),
            )
            message = Message.create("agent:alice", "still delivered")
            for number, expected in enumerate(("green", "green", "#7aa2f7")):
                await ui.refresh_team()
                await ui.show([(f"1-{number}", {"data": message.encode()})])
                assert rendered[-1] == expected
                assert (ui.presence is None) == (number == 1)
            assert output.getvalue().count("still delivered") == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [False, True])
def test_unknown_sender_refreshes_once_before_rendering_the_batch(
    tmp_path, monkeypatch, failure
):
    from toolang.teaming.errors import BackendUnavailable

    async def scenario():
        async with talk_app(tmp_path) as (ui, _):
            ui.client.team.return_value = [
                {"member": "agent:alice", "color": "red", "deadline": 1}
            ]
            await ui.refresh_team()
            ui.client.team.reset_mock()
            ui.client.team.return_value = [
                {"member": "agent:alice", "color": "red", "deadline": 1},
                {"member": "agent:bob", "color": "green", "deadline": 1},
            ]
            if failure:
                ui.client.team.side_effect = BackendUnavailable("offline")
            rendered = []
            render = tui.message_block

            def block(message, *args, color=None):
                rendered.append((message.sender, color))
                return render(message, *args, color=color)

            monkeypatch.setattr(tui, "message_block", block)
            output = StringIO()
            monkeypatch.setattr(
                tui,
                "terminal_console",
                lambda *, width: Console(file=output, width=width, color_system=None),
            )
            messages = [
                Message.create("agent:alice", "Known sender"),
                Message.create("agent:bob", "New sender first message"),
                Message.create("agent:bob", "New sender second message"),
            ]
            await ui.show(
                [
                    (f"1-{index}", {"data": message.encode()})
                    for index, message in enumerate(messages)
                ]
            )
            ui.client.team.assert_awaited_once()
            assert rendered == [
                ("agent:alice", "red"),
                ("agent:bob", None if failure else "green"),
                ("agent:bob", None if failure else "green"),
            ]
            assert ui.cursor == "1-2"
            assert all(message.body in output.getvalue() for message in messages)
            if not failure:
                await ui.show([("1-3", {"data": messages[-1].encode()})])
                ui.client.team.assert_awaited_once()

    asyncio.run(scenario())


pytestmark = pytest.mark.usefixtures("fixed_conversation_ids")
