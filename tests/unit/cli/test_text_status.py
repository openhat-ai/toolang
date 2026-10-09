"""Text footer geometry, viewer identity, and connection-state transitions."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from prompt_toolkit.formatted_text import fragment_list_to_text

from tests.unit.cli.test_text_layout import text_app
from toolang.cli.common.execution_progress.formatting import display_width
from toolang.cli.toolang.commands.text.status import conversation_status, status_line
from toolang.teaming.client import HubClient
from toolang.teaming.errors import HubIdentityChanged, MessagingError
from toolang.teaming.schemas import Conversation, HubConnection


@pytest.mark.parametrize("width", [1, 2, 3, 4, 5, 8, 12, 20, 40, 60, 120])
@pytest.mark.parametrize("right", ["bryan", "爱丽丝", "Reconnecting…"])
def test_status_sides_fit_cells_and_preserve_margins(width, right):
    fragments = status_line(
        [("class:status.online", "@中文e\u0301")], right, width=width, warning=False
    )
    rendered = fragment_list_to_text(fragments)
    assert display_width(rendered) == width
    assert "\n" not in rendered
    if width >= 5:
        assert rendered.startswith("  ") and rendered.endswith("  ")
    if width >= 20:
        assert rendered.endswith(right + "  ")
    if width >= 40:
        assert rendered.startswith("  @中文e\u0301")


def test_status_sides_sanitize_controls_and_keep_warning_local():
    fragments = status_line(
        [("class:status", "#\x1b]52;c;secret\x07dev(1/2)")],
        "Reconnect\x1b[2J\nnow",
        width=80,
        warning=True,
    )
    text = fragment_list_to_text(fragments)
    assert "\x1b" not in text and "secret" not in text and "\n" not in text
    assert ("class:status.warning", "Reconnect now") in fragments
    assert ("class:status", "#dev(1/2)") in fragments
    assert text.endswith("Reconnect now  ")


def test_clipping_preserves_online_name_style_and_prioritizes_login():
    fragments = status_line(
        [("class:status.online", "@alice"), ("class:status", ",bob")],
        "bryan",
        width=14,
        warning=False,
    )
    assert fragment_list_to_text(fragments) == "  @al… bryan  "
    assert ("class:status.online", "@al") in fragments
    assert ("class:status", "…") in fragments


@pytest.mark.parametrize(
    "conversation,online,expected,green",
    [
        (
            Conversation("group:dm_alice", "direct", ("human:bryan", "agent:alice")),
            {"agent:alice"},
            "@alice",
            ["@alice"],
        ),
        (
            Conversation("group:dm_alice", "direct", ("human:bryan", "agent:alice")),
            set(),
            "@alice",
            [],
        ),
        (
            Conversation("group:dm_alice", "direct", ("human:bryan", "agent:alice")),
            None,
            "@alice",
            [],
        ),
        (
            Conversation("group:dm_pair", "direct", ("agent:bob", "agent:alice")),
            {"agent:alice"},
            "alice,bob",
            ["alice"],
        ),
        (
            Conversation("group:dm_pair", "direct", ("agent:bob", "agent:alice")),
            {"agent:bob"},
            "alice,bob",
            ["bob"],
        ),
        (
            Conversation("group:dm_pair", "direct", ("agent:bob", "agent:alice")),
            {"agent:alice", "agent:bob"},
            "alice,bob",
            ["alice", "bob"],
        ),
        (
            Conversation("group:dm_pair", "direct", ("agent:bob", "agent:alice")),
            None,
            "alice,bob",
            [],
        ),
        (
            Conversation(
                "group:gc_dev", "group", ("human:bryan", "agent:alice", "agent:bob")
            ),
            {"agent:alice", "agent:bob", "agent:outsider"},
            "#dev(2/3)",
            ["2"],
        ),
        (
            Conversation(
                "group:dev", "group", ("human:bryan", "agent:alice", "agent:bob")
            ),
            set(),
            "#dev(0/3)",
            [],
        ),
        (
            Conversation(
                "group:gc_dev", "group", ("human:bryan", "agent:alice", "agent:bob")
            ),
            None,
            "#dev(?/3)",
            [],
        ),
    ],
)
def test_conversation_formats_color_only_online_names_or_positive_count(
    tmp_path, conversation, online, expected, green
):
    async def scenario():
        async with text_app(tmp_path, conversation=conversation) as (ui, _):
            fragments = conversation_status(conversation, "human:bryan", online)
            assert fragment_list_to_text(fragments) == expected
            colored = []
            for style, value, *_ in fragments:
                attrs = ui.app.style.get_attrs_for_style_str(style)
                assert not attrs.dim
                if attrs.color:
                    assert attrs.color == "ansigreen"
                    colored.append(value)
            assert colored == green

    asyncio.run(scenario())


@pytest.mark.parametrize("human", ["human:alice", "human:bryan"])
def test_footer_uses_viewer_identity_and_survives_configured_width(tmp_path, human):
    async def scenario():
        async with text_app(tmp_path, human=human) as (ui, output):
            ui.max_width = 72
            for columns in (200, 60, 180):
                output.columns = columns
                footer = fragment_list_to_text(ui.status_text())
                width = min(columns, 72)
                assert footer.startswith("  #dev(1/3)")
                assert footer.endswith(human.split(":")[1] + "  ")
                assert "from " not in footer
                assert display_width(footer) == width

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "connection", ["Connecting…", "Reconnecting…", "Stopped", "Reopen Text"]
)
def test_connection_state_replaces_identity_and_hides_stale_presence(
    tmp_path, connection
):
    async def scenario():
        async with text_app(tmp_path) as (ui, _):
            ui.connection = connection
            text = fragment_list_to_text(ui.status_text())
            assert text.endswith(connection + "  ")
            assert "bryan" not in text and "Connected" not in text
            assert text.startswith("  #dev(?/3)")
            assert not any(
                "status.online" in fragment[0] for fragment in ui.status_text()
            )
            ui.connection = "Connected"
            assert fragment_list_to_text(ui.status_text()).endswith("bryan  ")

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["read", "send"])
def test_changed_hub_identity_requests_reopen_and_preserves_draft(
    tmp_path, monkeypatch, operation
):
    async def scenario():
        config = HubConnection("http://hub", "old-token", "human:bryan", "test")
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                401, json={"detail": "Hub authentication required"}
            )
        )
        async with (
            text_app(tmp_path) as (ui, _),
            HubClient(config, transport=transport) as client,
        ):
            ui.client = client
            ui.prompt.replace_input("keep this draft")
            notice = AsyncMock()
            monkeypatch.setattr(ui, "print_notice", notice)
            if operation == "read":
                await ui.follow()
            else:
                await ui.send("keep this draft")
            footer = fragment_list_to_text(ui.status_text())
            assert footer.endswith("Reopen Text  ") and "bryan" not in footer
            assert footer.startswith("  #dev(?/3)")
            assert ui.prompt.buffer.text == ui.draft.read_text() == "keep this draft"
            notice.assert_awaited_once_with("Hub identity changed; reopen Text")
            with pytest.raises(HubIdentityChanged):
                await client.agents()

    asyncio.run(scenario())


def test_other_terminal_errors_stop_following_and_show_notice(tmp_path, monkeypatch):
    async def scenario():
        async with text_app(tmp_path) as (ui, _):
            ui.client.agents.side_effect = MessagingError("Group is unavailable")
            notice = AsyncMock()
            monkeypatch.setattr(ui, "print_notice", notice)
            await ui.follow()
            assert fragment_list_to_text(ui.status_text()).endswith("Stopped  ")
            notice.assert_awaited_once_with("Group is unavailable")

    asyncio.run(scenario())


def test_draft_failure_remains_visible_when_connected(tmp_path, monkeypatch):
    def fail_write(*_args):
        raise OSError("disk full")

    async def scenario():
        async with text_app(tmp_path) as (ui, _):
            monkeypatch.setattr(
                "toolang.cli.toolang.commands.text.tui.atomic_write_text",
                fail_write,
            )
            ui.prompt.replace_input("draft")
            assert fragment_list_to_text(ui.status_text()).endswith(
                "Draft could not be saved: disk full  "
            )
            assert (
                "class:status.warning",
                "Draft could not be saved: disk full",
            ) in ui.status_text()

    asyncio.run(scenario())
