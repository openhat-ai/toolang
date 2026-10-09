"""Talk footer geometry, viewer identity, and connection-state transitions."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from prompt_toolkit.formatted_text import fragment_list_to_text

from tests.unit.cli.test_talk_layout import talk_app
from toolang.cli.common.execution_progress.formatting import display_width
from toolang.cli.toolang.commands.talk.status import conversation_status, status_line
from toolang.teaming.client import HubClient
from toolang.teaming.errors import HubIdentityChanged, MessagingError
from toolang.teaming.schemas import Conversation, HubConnection


@pytest.mark.parametrize("width", [1, 2, 3, 4, 5, 8, 12, 20, 40, 60, 120])
@pytest.mark.parametrize("right", ["bryan", "爱丽丝", "Reconnecting…"])
def test_status_sides_fit_cells_and_preserve_margins(width, right):
    fragments = status_line(
        [("class:status", "@中文e\u0301")], right, width=width, warning=False
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
        [("class:status", "#\x1b]52;c;secret\x07dev(2)")],
        "Reconnect\x1b[2J\nnow",
        width=80,
        warning=True,
    )
    text = fragment_list_to_text(fragments)
    assert "\x1b" not in text and "secret" not in text and "\n" not in text
    assert ("class:status.warning", "Reconnect now") in fragments
    assert ("class:status", "#dev(2)") in fragments
    assert text.endswith("Reconnect now  ")


def test_clipping_preserves_name_style_and_prioritizes_login():
    fragments = status_line(
        [("class:status", "@alice"), ("class:status", ",bob")],
        "bryan",
        width=14,
        warning=False,
    )
    assert fragment_list_to_text(fragments) == "  @al… bryan  "
    assert ("class:status", "@al") in fragments
    assert ("class:status", "…") in fragments


@pytest.mark.parametrize(
    "conversation,expected",
    [
        (
            Conversation("group:dm_alice", "direct", ("human:bryan", "agent:alice")),
            "@alice",
        ),
        (
            Conversation("group:dm_pair", "direct", ("agent:bob", "agent:alice")),
            "@alice,bob",
        ),
        (
            Conversation(
                "group:gc_dev", "group", ("human:bryan", "agent:alice", "agent:bob")
            ),
            "#dev(3)",
        ),
        (
            Conversation("group:dev", "group", ("human:bryan", "agent:alice")),
            "#dev(2)",
        ),
        (Conversation("group:empty", "group", ()), "#empty(0)"),
        (
            Conversation("group:observed", "group", ("agent:alice",)),
            "#observed(1)",
        ),
    ],
)
def test_conversation_formats_dim_only_read_only_markers_without_presence(
    tmp_path, conversation, expected
):
    async def scenario():
        async with talk_app(tmp_path, conversation=conversation) as (ui, _):
            fragments = conversation_status(conversation, "human:bryan")
            assert fragment_list_to_text(fragments) == expected
            for style, value, *_ in fragments:
                attrs = ui.app.style.get_attrs_for_style_str(style)
                assert attrs.dim == (
                    value in {"@", "#"}
                    and not conversation.allows_sender("human:bryan")
                )
                assert not attrs.color

    asyncio.run(scenario())


@pytest.mark.parametrize("width", [60, 80, 120])
@pytest.mark.parametrize("canonical", ["group:gc_dev", "group:开发e\u0301"])
def test_full_canonical_id_is_centered_by_terminal_cells(width, canonical):
    fragments = status_line(
        [("class:status", "#dev(3)")],
        "bryan",
        center=canonical,
        width=width,
        warning=False,
    )
    text = fragment_list_to_text(fragments)
    assert text.startswith("  #dev(3)") and text.endswith("bryan  ")
    assert display_width(text) == width
    assert (
        display_width(text[: text.index(canonical)])
        == (width - display_width(canonical)) // 2
    )


@pytest.mark.parametrize("width", [1, 12, 25, 40, 60, 80, 120])
def test_long_ids_are_shown_whole_or_hidden_without_overlapping_errors(width):
    canonical = "group:12345678-1234-1234-1234-123456789012"
    fragments = status_line(
        [("class:status dim", "@"), ("class:status", "alice,bob" * 10)],
        "Send failed",
        center=canonical,
        width=width,
        warning=True,
    )
    text = fragment_list_to_text(fragments)
    assert display_width(text) == width
    if width >= 16:
        assert text.endswith("Send failed  ")
    if "group:" in text:
        assert canonical in text
        assert ("class:status", canonical) in fragments
    if width >= 80:
        assert canonical in text


@pytest.mark.parametrize("human", ["human:alice", "human:bryan"])
def test_footer_uses_viewer_identity_and_survives_configured_width(tmp_path, human):
    async def scenario():
        async with talk_app(tmp_path, human=human) as (ui, output):
            ui.max_width = 72
            for columns in (200, 60, 180):
                output.columns = columns
                footer = fragment_list_to_text(ui.status_text())
                width = min(columns, 72)
                assert footer.startswith("  #dev(3)")
                assert footer.endswith(human.split(":")[1] + "  ")
                assert "from " not in footer
                assert display_width(footer) == width

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "connection", ["Connecting…", "Reconnecting…", "Stopped", "Reopen Talk"]
)
def test_connection_state_replaces_identity_and_keeps_conversation_label(
    tmp_path, connection
):
    async def scenario():
        async with talk_app(tmp_path) as (ui, _):
            ui.connection = connection
            text = fragment_list_to_text(ui.status_text())
            assert text.endswith(connection + "  ")
            assert "bryan" not in text and "Connected" not in text
            assert text.startswith("  #dev(3)")
            ui.connection = "Connected"
            assert fragment_list_to_text(ui.status_text()).endswith("bryan  ")

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["read", "send"])
def test_changed_hub_identity_requests_reopen_and_preserves_draft(
    tmp_path, monkeypatch, operation
):
    async def scenario():
        config = HubConnection("http://hub", "human:bryan", "test")
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                409, json={"code": "hub_changed", "detail": "Hub identity changed"}
            )
        )
        async with (
            talk_app(tmp_path) as (ui, _),
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
            assert footer.endswith("Reopen Talk  ") and "bryan" not in footer
            assert footer.startswith("  #dev(3)")
            assert ui.prompt.buffer.text == ui.draft.read_text() == "keep this draft"
            notice.assert_awaited_once_with("Hub identity changed; reopen Talk")
            with pytest.raises(HubIdentityChanged):
                await client.agents()

    asyncio.run(scenario())


def test_other_terminal_errors_stop_following_and_show_notice(tmp_path, monkeypatch):
    async def scenario():
        async with talk_app(tmp_path) as (ui, _):
            ui.client.history.side_effect = MessagingError("Group is unavailable")
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
        async with talk_app(tmp_path) as (ui, _):
            monkeypatch.setattr(
                "toolang.cli.toolang.commands.talk.tui.atomic_write_text",
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
