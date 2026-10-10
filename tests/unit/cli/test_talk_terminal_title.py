"""Talk publishes terminal identity without changing tmux ownership marks."""

import asyncio

from tests.support.conversations import conversation_record
from unittest.mock import AsyncMock, Mock

import pytest

from tests.unit.cli.test_talk_layout import talk_app
from toolang.cli.common.execution_progress.formatting import display_width
from toolang.cli.toolang.commands.talk import tui


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize(
    "conversation,label",
    [
        (None, "#dev"),
        (
            conversation_record("dm_00000001", "dm", ("human:bryan", "agent:alice")),
            "@alice",
        ),
        (
            conversation_record("dm_00000001", "dm", ("agent:bob", "agent:alice")),
            "@alice,bob",
        ),
    ],
)
def test_talk_sets_osc_zero_title_and_clears_it_on_exit(
    tmp_path, monkeypatch, fails, conversation, label
):
    async def scenario():
        async with talk_app(tmp_path, conversation=conversation) as (ui, output):
            monkeypatch.setenv("TOOLANG_TMUX", "0")
            monkeypatch.setattr(tui.os, "isatty", lambda _fd: True)
            monkeypatch.setattr(output, "fileno", lambda: 1)
            monkeypatch.setattr(ui.app.input, "fileno", lambda: 0)
            write = Mock()
            monkeypatch.setattr(output, "write_raw", write)
            monkeypatch.setattr(output, "flush", Mock())
            monkeypatch.setattr(
                ui.app,
                "run_async",
                AsyncMock(side_effect=RuntimeError("stopped") if fails else None),
            )
            ui.prompt.replace_input("keep this draft")
            if fails:
                with pytest.raises(RuntimeError, match="stopped"):
                    await ui.run()
            else:
                await ui.run()
            assert [call.args[0] for call in write.call_args_list] == [
                f"\x1b]0;{label}\x07",
                "\x1b]0;\x07",
            ]
            assert ui.draft.read_text() == "keep this draft"

    asyncio.run(scenario())


def test_talk_does_not_emit_titles_on_non_tty_output(tmp_path, monkeypatch):
    async def scenario():
        async with talk_app(tmp_path) as (ui, output):
            monkeypatch.setattr(tui.os, "isatty", lambda _fd: False)
            monkeypatch.setattr(output, "fileno", lambda: 1)
            write = Mock()
            monkeypatch.setattr(output, "write_raw", write)
            monkeypatch.setattr(ui.app, "run_async", AsyncMock())
            await ui.run()
            write.assert_not_called()

    asyncio.run(scenario())


def test_title_sanitizes_controls_and_terminal_failures_are_nonfatal(
    tmp_path, monkeypatch
):
    async def scenario():
        async with talk_app(tmp_path) as (ui, output):
            monkeypatch.setattr(tui.os, "isatty", lambda _fd: True)
            monkeypatch.setattr(output, "fileno", lambda: 1)
            monkeypatch.setattr(ui.app.input, "fileno", lambda: 0)
            write = Mock()
            monkeypatch.setattr(output, "write_raw", write)
            assert ui.write_title("Talk\n\x1b]52;c;secret\x07" + "开发" * 100)
            title = write.call_args.args[0]
            assert title.startswith("\x1b]0;Talk ") and title.endswith("\x07")
            assert "secret" not in title and "\n" not in title
            assert title.count("\x1b") == title.count("\x07") == 1
            assert display_width(title[4:-1]) <= 120
            write.side_effect = OSError("terminal closed")
            assert not ui.write_title("Talk")

    asyncio.run(scenario())


pytestmark = pytest.mark.usefixtures("fixed_conversation_ids")
