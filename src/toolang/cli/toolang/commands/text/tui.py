"""Live input and retained/live messages on the terminal's normal screen."""

import asyncio
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, HorizontalAlign, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from rich.text import Text

from toolang.cli.common.console import terminal_console
from toolang.cli.common.execution_progress.config import DEFAULT_MAX_PROGRESS_WIDTH
from toolang.cli.common.input import InputBox
from toolang.cli.common.input_history import InputHistoryStore
from toolang.cli.common.scrollback import ScrollbackRenderer
from toolang.cli.common.terminal_surfaces import TerminalSurfaces
from toolang.common.files import atomic_write_text
from toolang.teaming.client import HubClient
from toolang.teaming.errors import (
    BackendUnavailable,
    HubIdentityChanged,
    MessagingError,
    SendUnconfirmed,
)
from toolang.teaming.schemas import Conversation, Message, target

from .rendering import display_text, message_block
from .status import conversation_name, status_line


class TextTui:
    def __init__(
        self,
        client: HubClient,
        conversation: Conversation,
        human: str,
        state: Path,
        surfaces: TerminalSurfaces,
        *,
        read_only: bool,
        max_width: int = DEFAULT_MAX_PROGRESS_WIDTH,
    ):
        self.client, self.group, self.human = client, conversation.id, human
        self.surfaces = surfaces
        self.read_only = read_only
        self.label = conversation_name(conversation, human)
        self.members = conversation.members
        self.online_count: int | None = None
        self.max_width = max_width
        self.draft = state / "draft.txt"
        self.connection = "Connecting…"
        self.status = ""
        self.pending = False
        self.cursor = "0-0"
        self.agents: set[str] = set()
        self.prompt = InputBox(
            self.invalidate,
            history_store=InputHistoryStore(state / "input.jsonl"),
            on_input=self.save_draft,
            normalize=lambda text: text,
            get_max_rows=lambda: max(
                3, self.app.output.get_size().rows - 1 - self._input_gap_rows()
            ),
            get_width=self.content_width,
        )
        if not read_only and self.draft.exists():
            self.prompt.replace_input(self.draft.read_text(encoding="utf-8"))
        keys = KeyBindings()
        focus = has_focus(self.prompt.buffer) & Condition(lambda: not self.read_only)

        @keys.add("enter", filter=focus)
        def send(_event: Any) -> None:
            if self.prompt.buffer.text.strip() and not self.pending:
                self.pending = True
                self.app.create_background_task(self.send(self.prompt.buffer.text))

        @keys.add("c-j", filter=focus)
        def newline(_event: Any) -> None:
            self.prompt._insert_newline()

        @keys.add("c-p", filter=focus)
        def previous(_event: Any) -> None:
            self.prompt._previous_history()

        @keys.add("c-n", filter=focus)
        def next_entry(_event: Any) -> None:
            self.prompt._next_history()

        @keys.add("up", filter=focus)
        def up(_event: Any) -> None:
            self.prompt.buffer.cursor_up()

        @keys.add("down", filter=focus)
        def down(_event: Any) -> None:
            self.prompt.buffer.cursor_down()

        @keys.add("c-c")
        @keys.add("c-q")
        @keys.add(
            "c-d",
            filter=Condition(lambda: self.read_only or not self.prompt.has_input()),
        )
        def quit_app(_event: Any) -> None:
            self.app.exit()

        @keys.add("c-l")
        def clear(_event: Any) -> None:
            self.app.renderer.clear()

        footer = Window(
            FormattedTextControl(self.status_text, focusable=True),
            height=1,
            wrap_lines=False,
        )
        controls = (
            []
            if read_only
            else [Window(height=self._input_gap_rows), self.prompt.container()]
        )
        self.app: Application[None] = Application(
            layout=Layout(
                VSplit(
                    [
                        HSplit(
                            [*controls, footer],
                            width=self.content_width,
                        ),
                    ],
                    align=HorizontalAlign.LEFT,
                ),
                focused_element=footer if read_only else self.prompt.buffer,
            ),
            key_bindings=keys,
            full_screen=False,
            erase_when_done=True,
            mouse_support=False,
            refresh_interval=0.5,
            style=Style.from_dict(
                {
                    "input": f"bg:{surfaces.input_background}",
                    "input.placeholder": "dim",
                    "control.run": "bg:ansibrightcyan",
                    "status": "dim",
                    "status.warning": "ansiyellow",
                }
            ),
        )
        self.app.renderer = ScrollbackRenderer(
            self.app.renderer.style,
            self.app.output,
            full_screen=False,
            mouse_support=False,
            cpr_not_supported_callback=self.app.cpr_not_supported_callback,
        )

    def invalidate(self) -> None:
        if hasattr(self, "app"):
            self.app.invalidate()

    def content_width(self) -> int:
        return max(1, min(self.app.output.get_size().columns, self.max_width))

    def _input_gap_rows(self) -> int:
        # Keep the three-row input and footer usable in very short terminals.
        return int(self.app.output.get_size().rows >= 5)

    def status_text(self) -> StyleAndTextTuples:
        connected = self.connection == "Connected"
        warning = False
        if not connected:
            left = self.connection
            warning = self.connection != "Connecting…"
        elif self.status not in {"", "Sending…", "Sent"}:
            left, warning = self.status, True
        else:
            left = f"from {target(self.human).name}"
            if self.read_only:
                left += " · read-only"
        online = (
            self.online_count if connected and self.online_count is not None else "?"
        )
        return status_line(
            left,
            self.label,
            f"{online}/{len(self.members)}",
            width=self.content_width(),
            warning=warning,
        )

    async def refresh_directory(self) -> None:
        agents = set(await self.client.agents())
        groups = await self.client.contacts()
        info = next((info for info in groups if info["group"] == self.group), None)
        if info is None:
            raise MessagingError(f"Unknown group: {self.group}")
        self.agents = agents
        self.members = tuple(info["members"])
        self.online_count = len(agents.intersection(info["online"], self.members))
        self.invalidate()

    def save_draft(self) -> None:
        if self.read_only:
            return
        try:
            atomic_write_text(self.draft, self.prompt.buffer.text)
        except OSError as exc:
            self.status = f"Draft could not be saved: {exc}"
        self.invalidate()

    async def send(self, body: str) -> None:
        if self.read_only:
            return
        self.status = "Sending…"
        self.invalidate()
        try:
            await self.client.send(self.group, body=body)
        except MessagingError as exc:
            if isinstance(exc, HubIdentityChanged):
                self.connection = "Reopen Text"
            self.status = (
                "Send not confirmed"
                if isinstance(exc, SendUnconfirmed)
                else "Send failed"
            )
            await self.print_notice(str(exc))
        else:
            self.connection = "Connected"
            self.status = "Sent"
            self.prompt.accept_submission(body)
            self.save_draft()
        finally:
            self.pending = False
            self.invalidate()

    async def print_notice(self, text: str) -> None:
        await run_in_terminal(
            lambda: terminal_console(width=self.content_width()).print(
                Text(display_text(text), style="yellow")
            )
        )

    async def show(self, entries: list[tuple[str, dict[str, str]]]) -> None:
        if not entries:
            return

        def write() -> None:
            width = self.content_width()
            console = terminal_console(width=width)
            for sid, fields in entries:
                try:
                    message = Message.decode(fields["data"])
                except (KeyError, MessagingError):
                    console.print(
                        Text(f"Skipped malformed message at {sid}", style="yellow")
                    )
                else:
                    console.print(
                        message_block(
                            message, self.human, self.agents, width, self.surfaces
                        )
                    )

        await run_in_terminal(write)
        self.cursor = entries[-1][0]

    async def follow(self) -> None:
        initialized = False
        reconnecting = False
        delay = 0.5
        refresh_at = 0.0
        last_gap = None
        while True:
            try:
                if (
                    not initialized
                    or reconnecting
                    or asyncio.get_running_loop().time() >= refresh_at
                ):
                    await self.refresh_directory()
                    refresh_at = asyncio.get_running_loop().time() + 10
                if not initialized:
                    entries = await self.client.history(self.group)
                    if len(entries) == 200:
                        await self.print_notice(
                            "Showing the latest 200 retained messages"
                        )
                    await self.show(entries)
                    initialized = True
                else:
                    gap = await self.client.check_cursor(self.group, self.cursor)
                    if gap and gap != last_gap:
                        await self.print_notice(gap)
                    last_gap = gap
                    await self.show(
                        await self.client.read(self.group, after=self.cursor)
                    )
                if self.connection != "Connected":
                    self.connection = "Connected"
                    self.invalidate()
                reconnecting, delay = False, 0.5
            except BackendUnavailable:
                self.connection = "Reconnecting…"
                self.invalidate()
                reconnecting = True
                delay = min(delay * 2, 5)
            except MessagingError as exc:
                self.connection = (
                    "Reopen Text" if isinstance(exc, HubIdentityChanged) else "Stopped"
                )
                self.status = str(exc)
                self.invalidate()
                await self.print_notice(self.status)
                return
            await asyncio.sleep(delay)

    async def run(self) -> None:
        await self.app.run_async(
            pre_run=lambda: self.app.create_background_task(self.follow())
        )
        self.save_draft()
