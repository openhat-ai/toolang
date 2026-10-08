"""Text footer geometry, viewer identity, and connection-state transitions."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from prompt_toolkit.formatted_text import fragment_list_to_text

from tests.unit.cli.test_text_layout import text_app
from toolang.cli.common.execution_progress.formatting import display_width
from toolang.cli.toolang.commands.text.status import status_line
from toolang.teaming.client import HubClient
from toolang.teaming.errors import HubIdentityChanged, MessagingError
from toolang.teaming.schemas import HubConnection


@pytest.mark.parametrize("width", [1, 2, 3, 4, 5, 8, 12, 20, 40, 60, 120])
@pytest.mark.parametrize("left", ["from bryan", "from 爱丽丝", "Reconnecting…"])
def test_status_columns_fit_cells_and_preserve_margins(width, left):
    fragments = status_line(left, "gc_中文e\u0301", "2/10", width=width, warning=False)
    rendered = fragment_list_to_text(fragments)
    assert display_width(rendered) == width
    assert "\n" not in rendered
    if width >= 5:
        assert rendered.startswith("  ") and rendered.endswith("  ")
    if width >= 12:
        assert rendered.endswith("2/10  ")
    if width >= 40:
        before, after = rendered.split("gc_中文e\u0301")
        assert display_width(before) == (width - display_width("gc_中文e\u0301")) // 2
        assert after.endswith("2/10  ")


def test_status_columns_sanitize_controls_and_keep_warning_local():
    fragments = status_line(
        "Reconnect\x1b[2J\nnow",
        "gc_\x1b]52;c;secret\x07safe",
        "1/2",
        width=80,
        warning=True,
    )
    text = fragment_list_to_text(fragments)
    assert "\x1b" not in text and "secret" not in text and "\n" not in text
    assert ("class:status.warning", "Reconnect now") in fragments
    assert ("class:status", "gc_safe") in fragments
    assert ("class:status", "1/2") in fragments


@pytest.mark.parametrize("human", ["human:alice", "human:bryan"])
def test_footer_uses_viewer_identity_and_survives_configured_width(tmp_path, human):
    async def scenario():
        async with text_app(tmp_path, human=human) as (ui, output):
            ui.max_width = 72
            for columns in (200, 60, 180):
                output.columns = columns
                footer = fragment_list_to_text(ui.status_text())
                width = min(columns, 72)
                assert footer.startswith(f"  from {human.split(':')[1]}")
                assert footer.index(ui.label) == (width - len(ui.label)) // 2
                assert footer.endswith("1/3  ")
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
            assert text.startswith(f"  {connection}")
            assert "from bryan" not in text and "Connected" not in text
            assert text.endswith("?/3  ")
            ui.connection = "Connected"
            assert fragment_list_to_text(ui.status_text()).startswith("  from bryan")

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
            assert footer.startswith("  Reopen Text") and "from bryan" not in footer
            assert footer.endswith("?/3  ")
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
            assert fragment_list_to_text(ui.status_text()).startswith("  Stopped")
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
            assert fragment_list_to_text(ui.status_text()).startswith(
                "  Draft could not be saved: disk full"
            )
            assert (
                "class:status.warning",
                "Draft could not be saved: disk full",
            ) in ui.status_text()

    asyncio.run(scenario())
